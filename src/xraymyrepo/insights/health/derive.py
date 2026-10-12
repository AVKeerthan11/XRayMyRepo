"""``health``: snapshot document -> health ``Derivation`` (findings and metrics only).

Two kinds of findings, kept apart by their basis:

* **Observed coverage gaps** (``observed``): restatements of what the extractors
  could not see, citing the source spans of the issues and unresolved references.

      health.coverage.blind_spot       a file whose dependencies are not fully known

* **Inferred structural findings** (``inferred``): computed over the dependency graph.

      health.import.cycle              files that depend on each other (file level; the
                                       architecture derivation reports group cycles)
      health.dependency.hub            unusually many dependent files (fan-in)
      health.dependency.fan_out        unusually many dependencies (fan-out)
      health.module.unreferenced       no repository file depends on it
      health.dependency.unused_declared a declared requirement nothing imports

Claims of *absence* (unreferenced, unused) are only as good as what was seen: they
get ``high`` confidence only when no considered file has hidden imports (unresolved
``dynamic``/``not_found`` imports, or a failed/partial parse), else ``low``.

Metrics (``health.metrics``): ``fan_in`` / ``fan_out`` per considered file (distinct
repository files), and coverage totals on the repository node.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable

from pydantic import ValidationError

from xraymyrepo.cim import (
    REPOSITORY_KEY,
    Basis,
    Confidence,
    Derivation,
    DerivationRun,
    DerivedEvidence,
    EdgeKind,
    EdgeRef,
    ExtractionStatus,
    Finding,
    FindingSeverity,
    NodeMetric,
    ReferenceKind,
    RequiresAttributes,
    Role,
    SnapshotDocument,
    SnapshotStatus,
    SpanEvidence,
    UnresolvedReason,
)
from xraymyrepo.cim.enums import EXTRACTED_STATUSES
from xraymyrepo.cim.evidence import FactEvidence, SpanFields
from xraymyrepo.cim.keys import decode_path
from xraymyrepo.cim.nodes import ECOSYSTEM_LANGUAGES, Node, PackageAttributes
from xraymyrepo.cim.provenance import Provenance
from xraymyrepo.graph import (
    DerivationContractError,
    FileGraph,
    strongly_connected,
    validate_against,
)

from .params import ALGORITHM, ALGORITHM_VERSION, HealthParams

RULE_CYCLE = "health.import.cycle"
RULE_HUB = "health.dependency.hub"
RULE_FAN_OUT = "health.dependency.fan_out"
RULE_UNREFERENCED = "health.module.unreferenced"
RULE_BLIND_SPOT = "health.coverage.blind_spot"
RULE_UNUSED = "health.dependency.unused_declared"
RULE_METRICS = "health.metrics"

_HIDING_REASONS = frozenset({UnresolvedReason.DYNAMIC, UnresolvedReason.NOT_FOUND})
_UNPARSED = frozenset({ExtractionStatus.FAILED, ExtractionStatus.PARTIAL})


def _provenance(rule: str) -> Provenance:
    return Provenance(producer=ALGORITHM, rule=rule)


def _derived(
    rule: str, basis: Basis, node_keys: Iterable[str] = (), edge_refs: Iterable[EdgeRef] = ()
) -> DerivedEvidence:
    return DerivedEvidence(
        type="derived",
        basis=basis,
        provenance=_provenance(rule),
        node_keys=tuple(node_keys),
        edge_refs=tuple(edge_refs),
    )


def _span(rule: str, file_key: str, span: SpanFields) -> SpanEvidence:
    return SpanEvidence(
        type="span",
        basis=Basis.OBSERVED,
        provenance=_provenance(rule),
        file_key=file_key,
        start_line=span.start_line,
        start_col=span.start_col,
        end_line=span.end_line,
        end_col=span.end_col,
    )


def _listing(keys: list[str], limit: int = 5) -> str:
    shown = ", ".join(keys[:limit])
    return shown + (f" and {len(keys) - limit} more" if len(keys) > limit else "")


class _Health:
    def __init__(self, doc: SnapshotDocument, params: HealthParams) -> None:
        self.p = params
        self.g = FileGraph(doc)
        p, g = params, self.g
        # Files whose source the rules can see: tests included (they can hide imports too).
        self.visible = [
            f for f in g.files if f.language in p.languages and g.has_origin(f, p.origins)
        ]
        # Files the structural rules are about.
        self.considered = [
            f
            for f in self.visible
            if f.extraction_status in EXTRACTED_STATUSES
            and not g.is_test(f, p.test_directories, p.test_file_patterns)
        ]
        self.considered_keys = {f.key for f in self.considered}
        self.edges = g.edges_of_kinds(p.dependency_kinds)
        self.dependents: dict[str, set[str]] = {}
        self.dependencies: dict[str, set[str]] = {}
        self.incoming: dict[str, list[EdgeRef]] = {}
        self.outgoing: dict[str, list[EdgeRef]] = {}
        for fe in self.edges:
            self.dependents.setdefault(fe.target_file, set()).add(fe.source_file)
            self.dependencies.setdefault(fe.source_file, set()).add(fe.target_file)
            self.incoming.setdefault(fe.target_file, []).append(fe.edge.ref)
            self.outgoing.setdefault(fe.source_file, []).append(fe.edge.ref)

        visible_keys = {f.key for f in self.visible}
        self.unresolved: dict[str, list[tuple[SpanFields, UnresolvedReason]]] = {}
        hiding: set[str] = set()
        for ref in g.doc.unresolved_references:
            if ref.file_key in visible_keys:
                self.unresolved.setdefault(ref.file_key, []).append((ref, ref.reason))
                if ref.ref_kind is ReferenceKind.IMPORT and ref.reason in _HIDING_REASONS:
                    hiding.add(ref.file_key)
        hiding |= {f.key for f in self.visible if f.extraction_status in _UNPARSED}
        self.hiding = sorted(hiding)
        """Files that may depend on something the snapshot cannot show."""

    def fan_in(self, key: str) -> int:
        return len(self.dependents.get(key, ()))

    def fan_out(self, key: str) -> int:
        return len(self.dependencies.get(key, ()))

    def absence_confidence(self, file: Node | None = None) -> Confidence:
        if self.hiding or (
            file is not None and file.extraction_status is not ExtractionStatus.FULL
        ):
            return Confidence.LOW
        return Confidence.HIGH

    def hidden_note(self) -> str:
        if not self.hiding:
            return ""
        return (
            f"; {len(self.hiding)} file(s) have unresolved dynamic/unknown imports or did not "
            f"parse, so a dependent may be hidden"
        )

    # -- rules ---------------------------------------------------------------

    def cycles(self) -> list[Finding]:
        inside = [
            fe
            for fe in self.edges
            if fe.source_file in self.considered_keys and fe.target_file in self.considered_keys
        ]
        adjacency: dict[str, list[str]] = {}
        for fe in inside:
            targets = adjacency.setdefault(fe.source_file, [])
            if fe.target_file not in targets:
                targets.append(fe.target_file)
        for targets in adjacency.values():
            targets.sort()
        findings = []
        for component in sorted(strongly_connected(sorted(self.considered_keys), adjacency)):
            if len(component) < 2:
                continue
            members = set(component)
            edges = [fe for fe in inside if fe.source_file in members and fe.target_file in members]
            resolved = all(fe.edge.basis is Basis.RESOLVED for fe in edges)
            findings.append(
                Finding(
                    rule=RULE_CYCLE,
                    severity=FindingSeverity.MEDIUM,
                    subject_keys=tuple(component),
                    message=f"{len(component)} files depend on each other in a cycle: "
                    f"{_listing(component)}",
                    basis=Basis.INFERRED,
                    confidence=Confidence.HIGH if resolved else Confidence.MEDIUM,
                    evidence=(
                        _derived(
                            RULE_CYCLE,
                            Basis.INFERRED,
                            component,
                            [fe.edge.ref for fe in edges][: self.p.evidence_sample],
                        ),
                    ),
                )
            )
        return findings

    def _threshold(self, values: list[int], minimum: int) -> int:
        ranked = sorted(values, reverse=True)
        if not ranked:
            return minimum
        top = ranked[max(0, math.ceil(len(ranked) * self.p.top_percent / 100) - 1)]
        return max(minimum, top)

    def hubs(self) -> list[Finding]:
        findings = []
        for rule, measure, edges, minimum, what in (
            (RULE_HUB, self.fan_in, self.incoming, self.p.min_fan_in, "is depended on by"),
            (RULE_FAN_OUT, self.fan_out, self.outgoing, self.p.min_fan_out, "depends on"),
        ):
            threshold = self._threshold([measure(f.key) for f in self.considered], minimum)
            for file in self.considered:
                count = measure(file.key)
                if count < threshold:
                    continue
                findings.append(
                    Finding(
                        rule=rule,
                        severity=FindingSeverity.LOW,
                        subject_keys=(file.key,),
                        message=f"{file.key} {what} {count} files (threshold {threshold})",
                        basis=Basis.INFERRED,
                        confidence=Confidence.HIGH,
                        evidence=(
                            _derived(
                                rule,
                                Basis.INFERRED,
                                [file.key],
                                edges.get(file.key, [])[: self.p.evidence_sample],
                            ),
                        ),
                    )
                )
        return findings

    def unreferenced(self) -> list[Finding]:
        findings = []
        for file in self.considered:
            name = decode_path(file.key).rsplit("/", 1)[-1]
            if self.fan_in(file.key) or name in self.p.root_module_names:
                continue
            if any(role is Role.ENTRYPOINT for role, _ in self.g.roles.get(file.key, ())):
                continue
            findings.append(
                Finding(
                    rule=RULE_UNREFERENCED,
                    severity=FindingSeverity.INFO,
                    subject_keys=(file.key,),
                    message=f"No repository file depends on {file.key}{self.hidden_note()}",
                    basis=Basis.INFERRED,
                    confidence=self.absence_confidence(file),
                    evidence=(_derived(RULE_UNREFERENCED, Basis.INFERRED, [file.key]),),
                )
            )
        return findings

    def blind_spots(self) -> list[Finding]:
        issues: dict[str, list[tuple[str, SpanFields | None]]] = {}
        for issue in self.g.doc.extraction_issues:
            if issue.file_key is not None:
                issues.setdefault(issue.file_key, []).append((issue.code, issue.span))
        findings = []
        for file in self.visible:
            refs = self.unresolved.get(file.key, [])
            unparsed = file.extraction_status in _UNPARSED
            if not refs and not unparsed:
                continue
            parts = []
            if unparsed:
                codes = sorted({code for code, _ in issues.get(file.key, [])})
                parts.append(
                    f"extraction {file.extraction_status}"
                    + (f" ({', '.join(codes)})" if codes else "")
                )
            if refs:
                reasons = Counter(reason.value for _, reason in refs)
                parts.append(
                    f"{len(refs)} unresolved reference(s) ("
                    + ", ".join(f"{r}: {n}" for r, n in sorted(reasons.items()))
                    + ")"
                )
            spans = [s for _, s in issues.get(file.key, []) if s is not None] + [s for s, _ in refs]
            evidence: list[FactEvidence] = [
                _span(RULE_BLIND_SPOT, file.key, s) for s in spans[: self.p.evidence_sample]
            ]
            if not evidence:
                evidence = [_derived(RULE_BLIND_SPOT, Basis.OBSERVED, [file.key])]
            findings.append(
                Finding(
                    rule=RULE_BLIND_SPOT,
                    severity=FindingSeverity.LOW,
                    subject_keys=(file.key,),
                    message=f"Dependencies of {file.key} are not fully known: {'; '.join(parts)}",
                    basis=Basis.OBSERVED,
                    confidence=Confidence.HIGH,
                    evidence=tuple(evidence),
                )
            )
        return findings

    def unused_declared(self) -> list[Finding]:
        projects = {
            n.key
            for n in self.g.doc.nodes
            if isinstance(n.attributes, PackageAttributes)
            and set(ECOSYSTEM_LANGUAGES.get(n.attributes.ecosystem, ())) & set(self.p.languages)
        }
        used = {e.target_key for e in self.g.doc.edges if e.kind in self.p.dependency_kinds}
        findings = []
        for edge in self.g.doc.edges:
            attrs = edge.attributes
            if (
                edge.kind is not EdgeKind.REQUIRES
                or edge.source_key not in projects
                or not isinstance(attrs, RequiresAttributes)
                or attrs.scope not in self.p.unused_declared_scopes
                or edge.target_key in used
            ):
                continue
            findings.append(
                Finding(
                    rule=RULE_UNUSED,
                    severity=FindingSeverity.LOW,
                    subject_keys=(edge.source_key, edge.target_key),
                    message=f"{edge.source_key} declares {edge.target_key} ({attrs.scope}), but no "
                    f"repository file imports it{self.hidden_note()}",
                    basis=Basis.INFERRED,
                    confidence=self.absence_confidence(),
                    evidence=(
                        _derived(RULE_UNUSED, Basis.INFERRED, [edge.target_key], [edge.ref]),
                    ),
                )
            )
        return findings

    def metrics(self, blind_spots: int) -> list[NodeMetric]:
        provenance = _provenance(RULE_METRICS)
        metrics = []
        for file in self.considered:
            metrics.append(
                NodeMetric(
                    node_key=file.key,
                    metric="fan_in",
                    value=float(self.fan_in(file.key)),
                    provenance=provenance,
                )
            )
            metrics.append(
                NodeMetric(
                    node_key=file.key,
                    metric="fan_out",
                    value=float(self.fan_out(file.key)),
                    provenance=provenance,
                )
            )
        for name, value in (
            ("health_files_considered", len(self.considered)),
            ("health_blind_spot_files", blind_spots),
            ("health_hidden_import_files", len(self.hiding)),
        ):
            metrics.append(
                NodeMetric(
                    node_key=REPOSITORY_KEY, metric=name, value=float(value), provenance=provenance
                )
            )
        return metrics


def health(doc: SnapshotDocument, params: HealthParams | None = None) -> Derivation:
    """Derive the health findings and metrics of one snapshot. Pure and deterministic;
    returned in canonical order with status ``complete``."""
    params = params or HealthParams()
    h = _Health(doc, params)
    blind_spots = h.blind_spots()
    findings = [*h.cycles(), *h.hubs(), *h.unreferenced(), *blind_spots, *h.unused_declared()]
    try:
        derivation = Derivation(
            run=DerivationRun(
                snapshot=doc.snapshot,
                algorithm=ALGORITHM,
                algorithm_version=ALGORITHM_VERSION,
                params_hash=params.params_hash(),
                status=SnapshotStatus.COMPLETE,
            ),
            findings=tuple(findings),
            metrics=tuple(h.metrics(len(blind_spots))),
        ).canonical()
    except ValidationError as exc:
        raise DerivationContractError(str(exc)) from exc
    validate_against(doc, derivation)
    return derivation
