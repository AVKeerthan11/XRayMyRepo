"""``change_risk``: comparison + its two snapshots -> ``ChangeRiskAssessment``. Pure and
deterministic.

Each changed file is a subject (``cim.change_risk``). Its factors, all integer and
each citing what it rests on (docs/insights/change-risk-v1.md):

    risk.change.kind        removed 2, modified 1, moved 1, added 0
    risk.change.interface   +2: a declaration removed, or its signature_hash changed
    risk.impact.dependents  +1 for >= 1 dependent non-test file, +2 for >= wide_dependents
    risk.impact.endpoints   +1: an endpoint is reached
    risk.change.sensitive_role  +1: a manifest/config/schema/entrypoint/orm_entity role
    risk.health.hub / risk.health.cycle  +1 each: the inline health finding
    risk.dependency.unresolved_after_removal  +2: an import of a surviving file that
                            resolved into a removed file no longer resolves (exact
                            recorded text, same producer)
    risk.test.no_dependent_test  +1: no test depends on it, when that absence is reliable
    risk.cap.test_file      a test file's level is capped at low (0 points)

Impact runs over ``to`` (``from`` for a removed file, counting only dependents that
still exist). Health runs inline (``ChangeRiskParams.health``), on ``to`` and, only
when a removed file needs it, on ``from``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from xraymyrepo.cim import (
    ChangeRiskAssessment,
    ChangeRiskRun,
    ClassificationFacet,
    Confidence,
    Derivation,
    DerivedEvidence,
    EdgeChangeKind,
    EdgeKind,
    EdgeRef,
    ExtractionStatus,
    ImportsAttributes,
    Incomparability,
    MatchMethod,
    NodeChange,
    NodeChangeKind,
    NodeKind,
    Origin,
    ReferenceKind,
    RiskCaveat,
    RiskEvidence,
    RiskFactor,
    RiskFactorRule,
    RiskItem,
    RiskLevel,
    Role,
    Side,
    SnapshotComparison,
    SnapshotDocument,
    SnapshotStatus,
    SpanEvidence,
    SpanRef,
    UnresolvedReason,
    confidence_of,
    level_for,
)
from xraymyrepo.cim._base import MAX_EVIDENCE_SAMPLE, Slug
from xraymyrepo.cim.coverage import UnresolvedReference
from xraymyrepo.cim.edges import Edge
from xraymyrepo.cim.enums import EXTRACTED_STATUSES, SYMBOL_NODE_KINDS
from xraymyrepo.graph import DependencyGraph, FileGraph

from . import comparison as comparison_algorithm
from .health import RULE_CYCLE, RULE_HUB, HealthParams, health
from .impact import ImpactParams, ImpactResult, impact

ALGORITHM = "change_risk"
ALGORITHM_VERSION = "1.0.0"

KIND_POINTS = {
    NodeChangeKind.REMOVED: 2,
    NodeChangeKind.MODIFIED: 1,
    NodeChangeKind.MOVED: 1,
    NodeChangeKind.ADDED: 0,
    NodeChangeKind.UNCHANGED: 0,
    NodeChangeKind.UNDETERMINED: 0,
}
INTERFACE_POINTS = 2
UNRESOLVED_AFTER_REMOVAL_POINTS = 2
PYTHON_NAMESPACE_RULE = "py.import.namespace_package"
"""The Python resolver's rule for ``from ns import name`` in a namespace package; its
unresolved ``raw_text`` is ``specifier.name`` (docs/analyzer/python-v1.md §4)."""

_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


class ChangeRiskParams(BaseModel):
    """Every input of an assessment besides the comparison, its snapshots and the code
    version; all of it is in ``params_hash``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    medium_at: int = Field(default=2, ge=1)
    high_at: int = Field(default=4, ge=2)
    wide_dependents: int = Field(default=10, ge=1)
    """Dependent files from which ``risk.impact.dependents`` adds 2 points."""
    max_depth: int = Field(default=3, ge=0, le=10)
    max_nodes: int = Field(default=10_000, ge=1, le=100_000)
    subject_origins: tuple[Origin, ...] = (Origin.SOURCE,)
    """Changed files are assessed only when every origin they have is one of these."""
    sensitive_roles: tuple[Role, ...] = (
        Role.MANIFEST,
        Role.CONFIG,
        Role.SCHEMA,
        Role.ENTRYPOINT,
        Role.ORM_ENTITY,
    )
    python_import_producers: tuple[Slug, ...] = ("python-extractor",)
    """Producers whose unresolved ``from ns import name`` in a namespace package records
    ``ns.name`` under ``PYTHON_NAMESPACE_RULE`` (docs/analyzer/python-v1.md §4)."""
    health: HealthParams = HealthParams()
    """Health computed inline; also the dependency kinds and test patterns used here."""

    @model_validator(mode="after")
    def _thresholds(self) -> ChangeRiskParams:
        if self.high_at <= self.medium_at:
            raise ValueError("high_at must be greater than medium_at")
        return self

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = self.model_dump(mode="json")
        return data

    def params_hash(self) -> str:
        canonical = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ChangeRiskContractError(ValueError):
    """Inputs that do not fit together, or an assessment that does not fit them: a bug
    or a misuse, never repaired."""


def python_unresolved_name(specifier: str, name: str) -> str:
    """How the Python extractor records ``from <specifier> import <name>`` when the
    specifier is a namespace package without ``name`` (``ns`` + ``x`` -> ``ns.x``,
    ``.`` + ``x`` -> ``.x``)."""
    return specifier + name if specifier.endswith(".") else f"{specifier}.{name}"


# ---------------------------------------------------------------------------
# One side


class _Side:
    def __init__(self, side: Side, doc: SnapshotDocument, params: ChangeRiskParams) -> None:
        self.side = side
        self.doc = doc.canonical()
        self.files = FileGraph(self.doc)
        self.graph = DependencyGraph.from_document(self.doc)
        self.nodes = self.doc.nodes_by_key
        self.p = params
        self._health: Derivation | None = None
        self.declared: dict[str, list[str]] = {}
        for node in self.doc.nodes:
            if node.kind in SYMBOL_NODE_KINDS:
                file_key = self.files.file_of(node.key)
                if file_key is not None:
                    self.declared.setdefault(file_key, []).append(node.key)
        self.not_found_imports: dict[str, list[UnresolvedReference]] = {}
        for ref in self.doc.unresolved_references:
            if ref.ref_kind is ReferenceKind.IMPORT and ref.reason is UnresolvedReason.NOT_FOUND:
                self.not_found_imports.setdefault(ref.file_key, []).append(ref)

    def file_of(self, key: str) -> str | None:
        return self.files.file_of(key)

    def is_file(self, key: str | None) -> bool:
        return key is not None and self.nodes[key].kind is NodeKind.FILE

    def is_test(self, file_key: str) -> bool:
        h = self.p.health
        return self.files.is_test(self.nodes[file_key], h.test_directories, h.test_file_patterns)

    def health(self) -> Derivation:
        """Computed once, on first use (``from`` only when a removed subject needs it)."""
        if self._health is None:
            self._health = health(self.doc, self.p.health)
        return self._health

    def impact(self, file_key: str) -> ImpactResult:
        h = self.p.health
        return impact(
            self.graph,
            [file_key],
            ImpactParams(
                max_depth=self.p.max_depth,
                max_nodes=self.p.max_nodes,
                dependency_kinds=h.dependency_kinds,
                test_directories=h.test_directories,
                test_file_patterns=h.test_file_patterns,
            ),
        )


# ---------------------------------------------------------------------------
# The algorithm


def _weakest(confidences: Iterable[Confidence]) -> Confidence:
    return min(confidences, key=_RANK.__getitem__, default=Confidence.HIGH)


def _caveats(found: Iterable[RiskCaveat]) -> tuple[RiskCaveat, ...]:
    order = {c: i for i, c in enumerate(RiskCaveat)}
    return tuple(sorted(set(found), key=order.__getitem__))


def _sample(evidence: list[RiskEvidence]) -> tuple[RiskEvidence, ...]:
    return tuple(evidence[:MAX_EVIDENCE_SAMPLE])


@dataclass
class _RemovalMatches:
    """A removed file's surviving dependents: how many imports could be checked, the
    exact matches, and the dependents with no exact match."""

    candidates: int = 0
    matched: list[tuple[Edge, str, list[UnresolvedReference]]] = field(default_factory=list)
    unmatched: set[str] = field(default_factory=set)


class _Assessment:
    def __init__(
        self,
        comparison: SnapshotComparison,
        before: SnapshotDocument,
        after: SnapshotDocument,
        params: ChangeRiskParams,
    ) -> None:
        self.c = comparison.canonical()
        self.a = _Side(Side.FROM, before, params)
        self.b = _Side(Side.TO, after, params)
        self.p = params
        self.comparable = self.c.run.comparable
        self.same_extractor = (
            Incomparability.EXTRACTOR_VERSION_DIFFERS not in self.c.run.incomparable
        )
        self.by_from = {n.from_key: n for n in self.c.nodes if n.from_key is not None}
        self.by_to = {n.to_key: n for n in self.c.nodes if n.to_key is not None}
        self._removal_matches: dict[str, _RemovalMatches] = {}

    # -- subjects ------------------------------------------------------------

    def file_row(self, row: NodeChange) -> NodeChange | None:
        """The node change of the file a changed node belongs to (None outside files)."""
        if row.to_key is not None:
            file_key = self.b.file_of(row.to_key)
            if file_key is not None:
                return self.by_to[file_key]
        if row.from_key is not None:
            file_key = self.a.file_of(row.from_key)
            if file_key is not None:
                return self.by_from[file_key]
        return None

    def subjects(self) -> dict[str, NodeChange]:
        found: dict[str, NodeChange] = {}
        for row in self.c.nodes:
            if row.change is not NodeChangeKind.UNCHANGED:
                file_row = self.file_row(row)
                if file_row is not None:
                    found[file_row.key] = file_row
        return dict(sorted(found.items()))

    def assessed(self, row: NodeChange) -> bool:
        side, key = self.home(row)
        return side.files.has_origin(side.nodes[key], self.p.subject_origins)

    def home(self, row: NodeChange) -> tuple[_Side, str]:
        """Where a subject is assessed: its file in ``to``, else in ``from``."""
        if self.b.is_file(row.to_key):
            assert row.to_key is not None
            return self.b, row.to_key
        assert row.from_key is not None and self.a.is_file(row.from_key)
        return self.a, row.from_key

    def undetermined_files(self) -> set[str]:
        """Subject keys touched by an undetermined node or edge change."""
        found = set()
        for row in self.c.nodes:
            if row.change is NodeChangeKind.UNDETERMINED:
                file_row = self.file_row(row)
                if file_row is not None:
                    found.add(file_row.key)
        for edge in self.c.edges:
            if edge.change is not EdgeChangeKind.UNDETERMINED:
                continue
            side = self.b if edge.to_ref is not None else self.a
            index = self.by_to if edge.to_ref is not None else self.by_from
            for end in (edge.ref.source_key, edge.ref.target_key):
                file_key = side.file_of(end)
                if file_key is not None:
                    found.add(index[file_key].key)
        return found

    # -- one subject ---------------------------------------------------------

    def item(self, row: NodeChange, undetermined: set[str]) -> RiskItem:
        side, key = self.home(row)
        from_file = row.from_key if self.a.is_file(row.from_key) else None
        to_file = row.to_key if self.b.is_file(row.to_key) else None
        caveats: set[RiskCaveat] = set()
        if not self.comparable:
            caveats.add(RiskCaveat.INCOMPARABLE_SNAPSHOTS)
        if row.key in undetermined:
            caveats.add(RiskCaveat.UNDETERMINED_CHANGES)
        for s, file_key in ((self.a, from_file), (self.b, to_file)):
            if file_key is not None and s.nodes[file_key].extraction_status is not (
                ExtractionStatus.FULL
            ):
                caveats.add(RiskCaveat.INCOMPLETE_EXTRACTION)

        factors = [self.kind(row, from_file, to_file)]
        factors += self.interface(from_file)
        is_test = side.is_test(key)
        result = side.impact(key)
        factors += self.impact_factors(row, side, key, result, is_test, caveats)
        factors += self.roles(side, key)
        factors += self.health(side, key)
        factors += self.unresolved_after_removal(row, from_file, caveats)
        if is_test:
            factors.append(
                RiskFactor(
                    rule=RiskFactorRule.TEST_FILE_CAP,
                    points=0,
                    value=1,
                    confidence=Confidence.HIGH,
                    message="a test file: its level is capped at low",
                    evidence=(RiskEvidence(side=side.side, node_keys=(key,)),),
                )
            )
        for factor in factors:
            caveats.update(factor.caveats)
        points = sum(f.points for f in factors)
        item_caveats = _caveats(caveats)
        return RiskItem(
            subject_key=row.key,
            change=row.change,
            # The cap is the test subject's own: no other item is touched by it.
            level=RiskLevel.LOW if is_test else level_for(points, self.p.medium_at, self.p.high_at),
            points=points,
            confidence=confidence_of(item_caveats),
            caveats=item_caveats,
            factors=tuple(factors),
        )

    def kind(self, row: NodeChange, from_file: str | None, to_file: str | None) -> RiskFactor:
        evidence = []
        if to_file is not None:
            evidence.append(RiskEvidence(side=Side.TO, node_keys=(to_file,)))
        if from_file is not None and from_file != to_file:  # removed, or moved from here
            evidence.append(RiskEvidence(side=Side.FROM, node_keys=(from_file,)))
        return RiskFactor(
            rule=RiskFactorRule.CHANGE_KIND,
            points=KIND_POINTS[row.change],
            value=1,
            confidence=row.confidence,
            message=f"the file is {row.change}",
            evidence=tuple(evidence),
        )

    def interface(self, from_file: str | None) -> list[RiskFactor]:
        """Declarations of the file in ``from`` that were removed, or whose signature
        changed (fingerprints compared only under one extractor version)."""
        if from_file is None:
            return []
        removed, changed = [], []
        for key in self.a.declared.get(from_file, ()):
            row = self.by_from[key]
            if row.change is NodeChangeKind.REMOVED:
                removed.append(row)
            elif row.to_key is not None and self.same_extractor:
                before, after = self.a.nodes[key], self.b.nodes[row.to_key]
                if (
                    before.signature_hash is not None
                    and after.signature_hash is not None
                    and before.signature_hash != after.signature_hash
                ):
                    changed.append(row)
        rows = removed + changed
        if not rows:
            return []
        evidence = [
            RiskEvidence(
                side=Side.FROM,
                node_keys=tuple(sorted(r.from_key for r in rows if r.from_key))[
                    :MAX_EVIDENCE_SAMPLE
                ],
            )
        ]
        if changed:
            evidence.append(
                RiskEvidence(
                    side=Side.TO,
                    node_keys=tuple(sorted(r.to_key for r in changed if r.to_key))[
                        :MAX_EVIDENCE_SAMPLE
                    ],
                )
            )
        return [
            RiskFactor(
                rule=RiskFactorRule.CHANGE_INTERFACE,
                points=INTERFACE_POINTS,
                value=len(rows),
                confidence=_weakest(r.confidence for r in rows),
                message=f"{len(removed)} declaration(s) removed, {len(changed)} with a changed "
                "signature",
                evidence=tuple(evidence),
            )
        ]

    def impact_factors(
        self,
        row: NodeChange,
        side: _Side,
        key: str,
        result: ImpactResult,
        is_test: bool,
        caveats: set[RiskCaveat],
    ) -> list[RiskFactor]:
        removed = row.change is NodeChangeKind.REMOVED

        def survives(item_key: str) -> bool:
            """For a removed file (impact over ``from``): the dependent still exists."""
            return not removed or self.by_from[item_key].to_key is not None

        reached = [i for i in result.items if i.distance > 0 or i.category == "endpoint"]
        dependents = [i for i in reached if i.category == "file" and survives(i.key)]
        endpoints = [i for i in reached if i.category == "endpoint"]
        tests = [i for i in reached if i.category == "test"]

        bounded: set[RiskCaveat] = set()
        if result.truncated or result.beyond_depth:
            bounded.add(RiskCaveat.IMPACT_BOUNDED)
        if result.blind_spots:
            bounded.add(RiskCaveat.HIDDEN_DEPENDENCIES)
        caveats.update(bounded)

        factors = []
        for rule, items, points in (
            (
                RiskFactorRule.IMPACT_DEPENDENTS,
                dependents,
                2 if len(dependents) >= self.p.wide_dependents else 1,
            ),
            (RiskFactorRule.IMPACT_ENDPOINTS, endpoints, 1),
        ):
            if not items:
                continue
            heuristic = (
                {RiskCaveat.HEURISTIC_PATHS}
                if any(i.confidence is not Confidence.HIGH for i in items)
                else set()
            )
            noun = "dependent file" if rule is RiskFactorRule.IMPACT_DEPENDENTS else "endpoint"
            factors.append(
                RiskFactor(
                    rule=rule,
                    points=points,
                    value=len(items),
                    confidence=_weakest(i.confidence for i in items),
                    message=f"{len(items)} {noun}(s) within {result.max_depth} hop(s)",
                    evidence=_sample(
                        [
                            RiskEvidence(side=side.side, node_keys=(i.key,), edge_refs=i.via)
                            for i in items
                        ]
                    ),
                    caveats=_caveats(bounded | heuristic),
                )
            )

        if (
            not is_test
            and not removed
            and tests
            and all(t.confidence is not Confidence.HIGH for t in tests)
        ):
            # Only heuristic paths show a dependent test, and that withholds a point: if
            # they were wrong the risk would be higher, so say so.
            caveats.add(RiskCaveat.HEURISTIC_PATHS)
        if not is_test and not removed and not tests:
            # An absence: scored only when nothing could hide a test.
            unreliable = _caveats(bounded)
            factors.append(
                RiskFactor(
                    rule=RiskFactorRule.NO_DEPENDENT_TEST,
                    points=0 if unreliable else 1,
                    value=0,
                    confidence=Confidence.LOW if unreliable else Confidence.HIGH,
                    message=(
                        "no dependent test seen, but tests may be hidden: not scored"
                        if unreliable
                        else "no test depends on this file"
                    ),
                    evidence=(RiskEvidence(side=side.side, node_keys=(key,)),),
                    caveats=unreliable,
                )
            )
        return factors

    def roles(self, side: _Side, key: str) -> list[RiskFactor]:
        found = [
            c
            for c in side.doc.classifications
            if c.facet is ClassificationFacet.ROLE
            and Role(c.value) in self.p.sensitive_roles
            and side.file_of(c.node_key) == key
        ]
        if not found:
            return []
        roles = sorted({c.value for c in found})
        return [
            RiskFactor(
                rule=RiskFactorRule.SENSITIVE_ROLE,
                points=1,
                value=len(roles),
                confidence=_weakest(c.confidence for c in found),
                message=f"role(s) {', '.join(roles)}",
                evidence=(
                    RiskEvidence(
                        side=side.side,
                        node_keys=tuple(sorted({c.node_key for c in found}))[:MAX_EVIDENCE_SAMPLE],
                    ),
                ),
            )
        ]

    def health(self, side: _Side, key: str) -> list[RiskFactor]:
        factors = []
        for rule, health_rule in (
            (RiskFactorRule.HEALTH_HUB, RULE_HUB),
            (RiskFactorRule.HEALTH_CYCLE, RULE_CYCLE),
        ):
            findings = [
                f for f in side.health().findings if f.rule == health_rule and key in f.subject_keys
            ]
            if not findings:
                continue
            evidence = [_risk_evidence(side.side, e) for f in findings for e in f.evidence]
            factors.append(
                RiskFactor(
                    rule=rule,
                    points=1,
                    value=len({k for f in findings for k in f.subject_keys}),
                    confidence=_weakest(f.confidence for f in findings),
                    message="; ".join(f.message for f in findings),
                    evidence=_sample(evidence),
                )
            )
        return factors

    def removal_matches(self, from_file: str) -> _RemovalMatches:
        """The exact matches of one removed file's dependents (computed once each)."""
        if from_file in self._removal_matches:
            return self._removal_matches[from_file]
        targets = set(self.a.graph.descendants(from_file))
        found = _RemovalMatches()
        for edge in self.a.doc.edges:
            if edge.kind is not EdgeKind.IMPORTS or edge.target_key not in targets:
                continue
            dependent = self.a.file_of(edge.source_key)
            if dependent is None or dependent == from_file:
                continue
            if self.by_from[dependent].match_method is not MatchMethod.SAME_KEY:
                continue  # moved or gone: relative specifiers mean something else now
            if not all(
                s.nodes[dependent].extraction_status in EXTRACTED_STATUSES for s in (self.a, self.b)
            ):
                continue
            found.candidates += 1
            refs = [
                r for r in self.b.not_found_imports.get(dependent, ()) if self.recorded(edge, r)
            ]
            if refs:
                found.matched.append((edge, dependent, refs))
            else:
                found.unmatched.add(dependent)
        self._removal_matches[from_file] = found
        return found

    def explained(self, dependent: str) -> int:
        """``not_found`` imports of ``dependent`` in ``to`` that some removed file's exact
        match explains."""
        refs = {
            id(r)
            for row in self.c.nodes
            if row.change is NodeChangeKind.REMOVED and self.a.is_file(row.from_key)
            for edge, d, matched in self.removal_matches(str(row.from_key)).matched
            if d == dependent
            for r in matched
        }
        return len(refs)

    def unresolved_after_removal(
        self, row: NodeChange, from_file: str | None, caveats: set[RiskCaveat]
    ) -> list[RiskFactor]:
        """For a removed file: an ``IMPORTS`` edge of a surviving file ``D`` resolved into
        it in ``from``, and an import of ``D`` with exactly the recorded text that edge's
        import has when its module is gone is ``not_found`` in ``to`` (``recorded``).

        Only a removed *file* leaves such a record: when a module still exists but a
        name is gone, the resolver resolves the import to the module and records
        nothing, so a removed declaration of a surviving file is not checked here.
        Weaker signals are caveats, never points."""
        if from_file is None or row.change is not NodeChangeKind.REMOVED:
            return []
        found = self.removal_matches(from_file)
        if not found.candidates:
            return []
        if not self.comparable:
            caveats.add(RiskCaveat.RESOLUTION_NOT_COMPARABLE)
            return []
        for dependent in sorted(found.unmatched):
            # New not_found imports that no exact match explains: maybe caused by the
            # removal, maybe not. A caveat, never points.
            new = len(self.b.not_found_imports.get(dependent, ())) - len(
                self.a.not_found_imports.get(dependent, ())
            )
            if new > self.explained(dependent):
                caveats.add(RiskCaveat.DEPENDENT_RESOLUTION_CHANGED)
        if not found.matched:
            return []
        evidence = []
        for edge, dependent, refs in found.matched:
            evidence.append(RiskEvidence(side=Side.FROM, edge_refs=(_ref(edge),)))
            evidence.append(
                RiskEvidence(
                    side=Side.TO,
                    node_keys=(dependent,),
                    spans=tuple(
                        SpanRef(
                            ref="span",
                            file_key=dependent,
                            start_line=r.start_line,
                            end_line=r.end_line,
                        )
                        for r in refs
                    )[:MAX_EVIDENCE_SAMPLE],
                )
            )
        dependents = sorted({d for _, d, _ in found.matched})
        return [
            RiskFactor(
                rule=RiskFactorRule.UNRESOLVED_AFTER_REMOVAL,
                points=UNRESOLVED_AFTER_REMOVAL_POINTS,
                value=len(found.matched),
                confidence=Confidence.HIGH,
                message=f"{len(found.matched)} import(s) that resolved into the removed file "
                f"no longer resolve, in {', '.join(dependents[:5])}",
                evidence=_sample(evidence),
            )
        ]

    def recorded(self, edge: Edge, ref: UnresolvedReference) -> bool:
        """``ref`` is exactly how ``edge``'s import is recorded once the module it
        resolved into is gone (same producer):

        * the specifier as written, the generic form (``import m``, ``from .m import x``);
        * ``specifier.name`` for a name the edge bound, only from a Python producer under
          its namespace-package rule (``from ns import mod`` when ``ns/mod.py`` is gone;
          docs/analyzer/python-v1.md §4).
        """
        attributes = edge.attributes
        assert isinstance(attributes, ImportsAttributes)
        producer = ref.provenance.producer
        if producer not in {e.provenance.producer for e in edge.evidence}:
            return False
        if ref.raw_text in attributes.specifiers:
            return True
        return (
            producer in self.p.python_import_producers
            and ref.provenance.rule == PYTHON_NAMESPACE_RULE
            and ref.raw_text
            in {
                python_unresolved_name(s, n)
                for s in attributes.specifiers
                for n in attributes.imported_names
            }
        )

    # -- the whole assessment ------------------------------------------------

    def run(self) -> ChangeRiskAssessment:
        subjects = self.subjects()
        assessed = {k: r for k, r in subjects.items() if self.assessed(r)}
        undetermined = self.undetermined_files()
        return ChangeRiskAssessment(
            run=ChangeRiskRun(
                comparison=self.c.run,
                algorithm=ALGORITHM,
                algorithm_version=ALGORITHM_VERSION,
                params_hash=self.p.params_hash(),
                medium_at=self.p.medium_at,
                high_at=self.p.high_at,
                status=SnapshotStatus.COMPLETE,
            ),
            items=tuple(self.item(r, undetermined) for r in assessed.values()),
            caveats=() if self.comparable else (RiskCaveat.INCOMPARABLE_SNAPSHOTS,),
            excluded_by_origin=len(subjects) - len(assessed),
        ).canonical()


def _ref(edge: Edge) -> EdgeRef:
    return EdgeRef.of(edge.kind, edge.source_key, edge.target_key)


def _risk_evidence(side: Side, evidence: object) -> RiskEvidence:
    """A health finding's evidence item, in the keys of its snapshot."""
    if isinstance(evidence, DerivedEvidence):
        return RiskEvidence(side=side, node_keys=evidence.node_keys, edge_refs=evidence.edge_refs)
    assert isinstance(evidence, SpanEvidence), evidence
    return RiskEvidence(
        side=side,
        spans=(
            SpanRef(
                ref="span",
                file_key=evidence.file_key,
                start_line=evidence.start_line,
                end_line=evidence.end_line,
            ),
        ),
    )


def change_risk(
    comparison: SnapshotComparison,
    before: SnapshotDocument,
    after: SnapshotDocument,
    params: ChangeRiskParams | None = None,
) -> ChangeRiskAssessment:
    """Assess a complete comparison of ``before`` -> ``after``. Returned in canonical
    order with status ``complete``, checked against the comparison and both snapshots."""
    params = params or ChangeRiskParams()
    if comparison.run.status is not SnapshotStatus.COMPLETE:
        raise ChangeRiskContractError("change risk is assessed on complete comparisons only")
    try:
        comparison_algorithm.validate_against(before, after, comparison)
    except comparison_algorithm.ComparisonContractError as exc:
        raise ChangeRiskContractError(f"the comparison does not fit the snapshots: {exc}") from exc
    try:
        assessment = _Assessment(comparison, before, after, params).run()
    except ValidationError as exc:
        raise ChangeRiskContractError(str(exc)) from exc
    validate_against(comparison, before, after, assessment)
    return assessment


# ---------------------------------------------------------------------------
# Checks against the comparison and its snapshots


def validate_against(
    comparison: SnapshotComparison,
    before: SnapshotDocument,
    after: SnapshotDocument,
    assessment: ChangeRiskAssessment,
) -> None:
    """What ``ChangeRiskAssessment`` cannot check alone: the run names the comparison;
    every subject is a file row of it with its change; every evidence key, edge ref
    and span exists in the snapshot its side names."""

    def fail(message: str) -> NoReturn:
        raise ChangeRiskContractError(message)

    if assessment.run.comparison != comparison.run:
        fail("the assessment names another comparison")
    rows = {n.key: n for n in comparison.nodes}
    docs = {Side.FROM: before, Side.TO: after}
    for item in assessment.items:
        row = rows.get(item.subject_key)
        if row is None or row.change is not item.change:
            fail(f"{item.subject_key!r} is not a {item.change} row of the comparison")
        if NodeKind.FILE not in {
            n.kind
            for d, k in ((before, row.from_key), (after, row.to_key))
            if k is not None
            for n in (d.nodes_by_key[k],)
        }:
            fail(f"{item.subject_key!r} is not a file")
        for factor in item.factors:
            for evidence in factor.evidence:
                doc = docs[evidence.side]
                for key in evidence.node_keys:
                    if key not in doc.nodes_by_key:
                        fail(f"{factor.rule}: {key!r} is not a node of {evidence.side}")
                for ref in evidence.edge_refs:
                    if (ref.kind, ref.source_key, ref.target_key) not in doc.edges_by_ref:
                        fail(f"{factor.rule}: {ref.source_key!r} -> {ref.target_key!r} is not "
                             f"an edge of {evidence.side}")  # fmt: skip
                for span in evidence.spans:
                    if span.file_key not in doc.nodes_by_key:
                        fail(f"{factor.rule}: span in {span.file_key!r}, not a file of "
                             f"{evidence.side}")  # fmt: skip
