"""Architecture intelligence: derivation runs, group trees, group edges, layers, findings.

Everything here is *derived* from a snapshot by a named algorithm. Records of the
inferred lens, layers and findings carry ``basis = inferred``, a confidence and
the rule that produced them; snapshot facts are only ever cited (by key), never
restated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from xraymyrepo.cim import (
    Basis,
    Confidence,
    EdgeKind,
    EdgeRef,
    FindingSeverity,
    LensKind,
    SnapshotStatus,
)
from xraymyrepo.cim.evidence import DerivedEvidence, FactEvidence
from xraymyrepo.cim.provenance import Provenance

from .common import ApiModel, NodeBrief


class DerivationRunSummary(ApiModel):
    id: int
    snapshot_id: int
    algorithm: str
    algorithm_version: str
    params_hash: str
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


class LensSummary(ApiModel):
    name: str
    kind: LensKind
    """``inferred`` lenses hold inferred groups; ``directory`` lenses observed ones."""
    group_count: int
    member_count: int
    """Files assigned to a group of the lens; the rest are unassigned in this lens."""


class LayerCoverage(ApiModel):
    considered: int
    """Files the layering considered (its scope)."""
    classified: int
    unclassified: int
    """Considered, but no layer could be inferred: a valid outcome, not an error."""
    by_layer: dict[str, int]
    by_confidence: dict[Confidence, int]


class DerivationRunDetail(DerivationRunSummary):
    params: dict[str, Any]
    """Every rule table and threshold the run used (they hash to ``params_hash``)."""
    lenses: list[LensSummary]
    layer_coverage: LayerCoverage
    finding_counts: dict[str, int]
    """Findings per rule."""


class LayerMix(ApiModel):
    """Layers of the considered files in a group and all its descendants."""

    by_layer: dict[str, int]
    unclassified: int


class GroupBrief(ApiModel):
    key: str
    label: str | None


class GroupSummary(ApiModel):
    """A group as a tree item."""

    key: str
    lens: str
    label: str | None
    parent_key: str | None
    basis: Basis
    confidence: Confidence
    child_group_count: int
    member_count: int
    """Files assigned directly to this group (descendant groups' files excluded)."""
    metrics: dict[str, float]
    """``files`` (including descendants), ``internal_edges``, ``afferent``, ``efferent``
    and, when it has external dependencies, ``instability``."""
    layer_mix: LayerMix


class GroupDetail(GroupSummary):
    provenance: Provenance
    evidence: list[FactEvidence]
    ancestors: list[GroupBrief]
    """From the lens root down to the parent."""


class GroupEdgeView(ApiModel):
    """Leaf edges of one kind rolled up between two groups of one lens. Never averaged:
    ``basis_counts`` says how many underlying edges have each basis."""

    source_group_key: str
    target_group_key: str
    edge_kind: EdgeKind
    count: int
    basis_counts: dict[Basis, int]
    sample_edge_refs: list[EdgeRef]


class LayerAssignment(ApiModel):
    node_key: str
    layer: str
    basis: Basis
    confidence: Confidence
    provenance: Provenance
    evidence: list[DerivedEvidence]
    """One item per signal (framework import, role, name, dependency direction)."""


class NodeLayer(ApiModel):
    node: NodeBrief
    status: Literal["classified", "unclassified", "out_of_scope"] = Field(
        description="`unclassified`: considered, no layer inferred; "
        "`out_of_scope`: not considered by the layering (e.g. tests, generated code)."
    )
    assignment: LayerAssignment | None


class FindingView(ApiModel):
    id: int
    rule: str
    severity: FindingSeverity
    subject_keys: list[str]
    """Node and/or group keys."""
    message: str
    basis: Basis
    confidence: Confidence
    evidence: list[FactEvidence]


class MetricView(ApiModel):
    subject_key: str
    """A node key (file metrics, e.g. ``fan_in``) or a group key (group metrics)."""
    metric: str
    value: float
    provenance: Provenance
