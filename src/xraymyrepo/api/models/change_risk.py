"""Change risk: how much each changed file of a comparison could break other code.

An assessment is computed by a named algorithm from one stored comparison and its two
snapshots, and stored whole; these models only read it. Rules:
docs/insights/change-risk-v1.md.

A level is a **lower bound on what was observed**: factors only add points, and caveats
lower confidence without changing points, so the true risk can be higher, never lower.
``conclusive`` is true only for ``high`` confidence, no caveats and no capped level;
``low`` with ``conclusive: false`` means no risk was observed in what could be seen, and
is never evidence that a change is safe. ``capped`` marks a test file whose level is
capped at ``low`` by policy (``risk.cap.test_file``), whatever its points.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from xraymyrepo.cim import (
    Confidence,
    NodeChangeKind,
    RiskCaveat,
    RiskEvidence,
    RiskFactorRule,
    RiskLevel,
    SnapshotStatus,
)

from .common import ApiModel
from .comparison import ComparisonRunSummary

_CONCLUSIVE = (
    "High confidence, no caveat and not capped (a run: no capped item). A `low` level that "
    "is not conclusive means no risk was observed in what could be seen; it is not evidence "
    "that the change is safe."
)


class ChangeRiskRunSummary(ApiModel):
    id: int
    comparison_id: int
    algorithm: str
    algorithm_version: str
    params_hash: str
    medium_at: int = Field(description="Points from which an item is `medium`")
    high_at: int = Field(description="Points from which an item is `high`")
    caveats: list[RiskCaveat] = Field(
        description="Caveats of the whole run (`incomparable_snapshots` when the comparison is "
        "not fully comparable)."
    )
    excluded_by_origin: int = Field(
        description="Changed files not assessed because of their origin (e.g. docs)"
    )
    level: RiskLevel | None = Field(
        description="The highest item level, a lower bound on what was observed; null "
        "until complete."
    )
    confidence: Confidence | None = Field(
        description="The lowest item confidence, at most what the run's caveats allow; null "
        "until complete."
    )
    conclusive: bool | None = Field(description=f"{_CONCLUSIVE} Null until complete.")
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


class ChangeRiskCounts(ApiModel):
    """Items by level and by confidence; every value present, zeros included."""

    levels: dict[RiskLevel, int]
    confidences: dict[Confidence, int]


class ChangeRiskDetail(ChangeRiskRunSummary):
    comparison: ComparisonRunSummary
    params: dict[str, Any]
    counts: ChangeRiskCounts


class RiskFactorSummary(ApiModel):
    rule: RiskFactorRule
    points: int = Field(description="Points this factor adds (never negative)")
    value: int = Field(description="What the rule measured: dependents, declarations, matches")
    confidence: Confidence
    message: str
    caveats: list[RiskCaveat] = Field(
        description="Why this factor may be incomplete; an absence factor with 0 points says "
        "why it was not scored."
    )


class RiskFactorDetail(RiskFactorSummary):
    evidence: list[RiskEvidence] = Field(
        description="Node keys, edge refs and spans, each in the keys of the snapshot its "
        "`side` names (at most 20)."
    )


class _RiskItem(ApiModel):
    subject_key: str = Field(description="The file's key in `to`, else in `from`")
    change: NodeChangeKind = Field(description="The file's own node change")
    level: RiskLevel = Field(description="A lower bound on what was observed")
    points: int
    confidence: Confidence
    capped: bool = Field(
        description="A test file whose level is capped at `low` by policy "
        "(`risk.cap.test_file`); its points are not. A capped item is never conclusive."
    )
    conclusive: bool = Field(description=_CONCLUSIVE)
    caveats: list[RiskCaveat]


class RiskItemSummary(_RiskItem):
    factors: list[RiskFactorSummary] = Field(description="In rule order, without evidence")


class RiskItemDetail(_RiskItem):
    factors: list[RiskFactorDetail] = Field(description="In rule order, with their evidence")
