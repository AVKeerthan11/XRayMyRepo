"""Change risk: how much each changed file of a comparison could break other code.

An assessment is computed from one complete ``SnapshotComparison`` and its two
snapshots (``xraymyrepo.insights.change_risk``), identified by
``(comparison, algorithm, algorithm_version, params_hash)`` like a derivation run.

**Subjects** are files: every file with a changed node (the file itself, or a
declaration or endpoint in it), keyed like the comparison's node changes (the ``to``
key, else the ``from`` key).

**Points and level.** Each ``RiskFactor`` adds a non-negative integer number of
points; an item's points are their sum, and its level follows from the run's
thresholds: ``low`` below ``medium_at``, ``medium`` below ``high_at``, ``high``
from it. A test-file subject's level is capped at ``low`` (``risk.cap.test_file``);
the cap never applies to another subject. The assessment's level is the highest
item level.

**A level is a lower bound on what was observed.** Factors only add points.
``RiskCaveat`` s say why the snapshots may not show everything; they lower
confidence and never change points, so the true risk can be higher than the level,
never lower. An absence ("no test depends on it") is scored only when it is
reliable; otherwise the factor is listed with 0 points and its caveats. ``low``
with ``conclusive`` false means "no risk was observed in what could be seen", never
"safe".

**Conclusive** (derived, never stored): ``high`` confidence, no caveat, and not
``capped``. A capped test file's ``low`` is a policy, not what its points say, so it is
never conclusive; an assessment is conclusive only when every item is.

**Confidence** of an item is the lowest implied by its caveats
(``CAVEAT_CONFIDENCE``); ``high`` without caveats.

**Evidence** is ``RiskEvidence``: node keys, edge refs and source spans, each tagged
with the snapshot (``Side``) whose keys it uses.
"""

from __future__ import annotations

from collections import Counter

from pydantic import Field, model_validator

from ._base import MAX_EVIDENCE_SAMPLE, CIMModel, SemVer, Sha256, Slug, Text
from .comparison import ComparisonRun
from .enums import (
    Confidence,
    NodeChangeKind,
    NodeKind,
    RiskCaveat,
    RiskFactorRule,
    RiskLevel,
    Side,
    SnapshotStatus,
)
from .keys import validate_key
from .refs import EdgeRef, SpanRef

CAVEAT_CONFIDENCE: dict[RiskCaveat, Confidence] = {
    RiskCaveat.INCOMPARABLE_SNAPSHOTS: Confidence.MEDIUM,
    RiskCaveat.UNDETERMINED_CHANGES: Confidence.LOW,
    RiskCaveat.INCOMPLETE_EXTRACTION: Confidence.LOW,
    RiskCaveat.HIDDEN_DEPENDENCIES: Confidence.MEDIUM,
    RiskCaveat.IMPACT_BOUNDED: Confidence.MEDIUM,
    RiskCaveat.HEURISTIC_PATHS: Confidence.MEDIUM,
    RiskCaveat.RESOLUTION_NOT_COMPARABLE: Confidence.MEDIUM,
    RiskCaveat.DEPENDENT_RESOLUTION_CHANGED: Confidence.MEDIUM,
}
"""The confidence each caveat allows at most."""

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_RULE_ORDER = {rule: i for i, rule in enumerate(RiskFactorRule)}
_CAVEAT_ORDER = {caveat: i for i, caveat in enumerate(RiskCaveat)}


def confidence_of(caveats: tuple[RiskCaveat, ...]) -> Confidence:
    """The lowest confidence the caveats allow; ``high`` without caveats."""
    return min(
        (CAVEAT_CONFIDENCE[c] for c in caveats),
        key=_CONFIDENCE_RANK.__getitem__,
        default=Confidence.HIGH,
    )


def level_for(points: int, medium_at: int, high_at: int) -> RiskLevel:
    if points >= high_at:
        return RiskLevel.HIGH
    if points >= medium_at:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _caveats_in_order(caveats: tuple[RiskCaveat, ...], what: str) -> None:
    if list(caveats) != sorted(set(caveats), key=_CAVEAT_ORDER.__getitem__):
        raise ValueError(f"{what} caveats must be unique, in enum order")


class ChangeRiskRun(CIMModel):
    comparison: ComparisonRun
    """The assessed comparison's identity (complete)."""
    algorithm: Slug
    algorithm_version: SemVer
    params_hash: Sha256
    medium_at: int = Field(ge=1)
    high_at: int
    """Thresholds of the level (also in the params): recorded so every level can be
    checked against its points."""
    status: SnapshotStatus = SnapshotStatus.PENDING

    @model_validator(mode="after")
    def _consistent(self) -> ChangeRiskRun:
        if self.comparison.status is not SnapshotStatus.COMPLETE:
            raise ValueError("change risk is assessed on complete comparisons only")
        if self.high_at <= self.medium_at:
            raise ValueError("high_at must be greater than medium_at")
        return self


class RiskEvidence(CIMModel):
    """Concrete records a factor rests on, in the keys of one snapshot."""

    side: Side
    node_keys: tuple[str, ...] = ()
    edge_refs: tuple[EdgeRef, ...] = ()
    spans: tuple[SpanRef, ...] = ()

    @model_validator(mode="after")
    def _non_empty(self) -> RiskEvidence:
        if not (self.node_keys or self.edge_refs or self.spans):
            raise ValueError("risk evidence must cite at least one node, edge or span")
        return self


class RiskFactor(CIMModel):
    rule: RiskFactorRule
    points: int = Field(ge=0)
    value: int = Field(ge=0)
    """What the rule measured: dependents, declarations, roles, matches; 1 for the
    change itself and the cap."""
    confidence: Confidence
    message: Text
    evidence: tuple[RiskEvidence, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_SAMPLE)
    caveats: tuple[RiskCaveat, ...] = ()
    """Why this factor may be incomplete; an absence factor that is not scored says why."""

    @model_validator(mode="after")
    def _consistent(self) -> RiskFactor:
        _caveats_in_order(self.caveats, "factor")
        if self.rule is RiskFactorRule.TEST_FILE_CAP and self.points:
            raise ValueError("the test-file cap adds no points")
        if self.rule is RiskFactorRule.NO_DEPENDENT_TEST and not self.points and not self.caveats:
            raise ValueError("an absence factor without points must say why (caveats)")
        return self


class RiskItem(CIMModel):
    subject_key: str
    """A file key: in ``to``, else in ``from`` (the comparison row's key)."""
    change: NodeChangeKind
    """The file's own node change (``unchanged`` when only its contents changed)."""
    level: RiskLevel
    points: int = Field(ge=0)
    confidence: Confidence
    caveats: tuple[RiskCaveat, ...] = ()
    factors: tuple[RiskFactor, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> RiskItem:
        validate_key(NodeKind.FILE, self.subject_key)
        _caveats_in_order(self.caveats, "item")
        rules = [f.rule for f in self.factors]
        if len(set(rules)) != len(rules):
            raise ValueError("a factor rule appears at most once per item")
        if rules[0] is not RiskFactorRule.CHANGE_KIND:
            raise ValueError("every item starts with its change (risk.change.kind)")
        if self.points != sum(f.points for f in self.factors):
            raise ValueError("an item's points are the sum of its factors' points")
        missing = {c for f in self.factors for c in f.caveats} - set(self.caveats)
        if missing:
            raise ValueError(f"factor caveats must be item caveats: {sorted(missing)}")
        if self.confidence is not confidence_of(self.caveats):
            raise ValueError("an item's confidence is the lowest its caveats allow")
        return self

    @property
    def capped(self) -> bool:
        """The level is capped at ``low`` (``risk.cap.test_file``); the points are not."""
        return any(f.rule is RiskFactorRule.TEST_FILE_CAP for f in self.factors)

    @property
    def conclusive(self) -> bool:
        """``high`` confidence, nothing that could hide more risk, and a level that says
        what the points say (not capped). A ``low`` level that is not conclusive is not
        evidence of safety."""
        return self.confidence is Confidence.HIGH and not self.caveats and not self.capped


class ChangeRiskSummary(CIMModel):
    """Items by level and by confidence; every value present, zeros included."""

    levels: dict[RiskLevel, int]
    confidences: dict[Confidence, int]


class ChangeRiskAssessment(CIMModel):
    """One change-risk run over a comparison and every item it assessed."""

    run: ChangeRiskRun
    items: tuple[RiskItem, ...] = ()
    caveats: tuple[RiskCaveat, ...] = ()
    """Caveats of the whole assessment (``incomparable_snapshots``)."""
    excluded_by_origin: int = Field(default=0, ge=0)
    """Changed files left out because their origin is not assessed (e.g. docs)."""

    @model_validator(mode="after")
    def _consistent(self) -> ChangeRiskAssessment:
        _caveats_in_order(self.caveats, "assessment")
        keys = [i.subject_key for i in self.items]
        repeated = sorted(k for k, n in Counter(keys).items() if n > 1)
        if repeated:
            raise ValueError(f"subjects appear more than once: {repeated}")
        comparable = self.run.comparison.comparable
        if (RiskCaveat.INCOMPARABLE_SNAPSHOTS in self.caveats) == comparable:
            raise ValueError("incomparable_snapshots is a caveat exactly when the comparison is")
        for item in self.items:
            if not comparable and RiskCaveat.INCOMPARABLE_SNAPSHOTS not in item.caveats:
                raise ValueError("every item of an incomparable comparison carries the caveat")
            expected = (
                RiskLevel.LOW
                if item.capped
                else level_for(item.points, self.run.medium_at, self.run.high_at)
            )
            if item.level is not expected:
                raise ValueError(
                    f"{item.subject_key!r}: {item.points} points make {expected}, not {item.level}"
                )
        return self

    # -- derived views -------------------------------------------------------

    @property
    def level(self) -> RiskLevel:
        """The highest item level; ``low`` when nothing was assessed."""
        return max((i.level for i in self.items), key=lambda x: x.rank, default=RiskLevel.LOW)

    @property
    def confidence(self) -> Confidence:
        """The lowest item confidence, and at most what the assessment's caveats allow."""
        return min(
            [confidence_of(self.caveats), *(i.confidence for i in self.items)],
            key=_CONFIDENCE_RANK.__getitem__,
        )

    @property
    def conclusive(self) -> bool:
        """``high`` confidence, no caveat, and no capped item: every item is conclusive."""
        return (
            self.confidence is Confidence.HIGH
            and not self.caveats
            and not any(i.capped for i in self.items)
        )

    def summary(self) -> ChangeRiskSummary:
        levels = Counter(i.level for i in self.items)
        confidences = Counter(i.confidence for i in self.items)
        return ChangeRiskSummary(
            levels={level: levels[level] for level in RiskLevel},
            confidences={c: confidences[c] for c in Confidence},
        )

    # -- canonical form ------------------------------------------------------

    def canonical(self) -> ChangeRiskAssessment:
        """Items by subject key, factors in rule order (evidence keeps its order)."""
        items = tuple(
            item.model_copy(
                update={"factors": tuple(sorted(item.factors, key=lambda f: _RULE_ORDER[f.rule]))}
            )
            for item in sorted(self.items, key=lambda i: i.subject_key)
        )
        return self.model_copy(update={"items": items})
