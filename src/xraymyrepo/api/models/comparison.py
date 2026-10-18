"""Snapshot comparison: what changed between two snapshots of one repository.

A comparison is computed by a named algorithm from two complete snapshots and stored
whole; these models only read it. Node changes cite nodes by key in their own
snapshot (``from_key`` in ``from``, ``to_key`` in ``to``), edge changes cite edges by
ref; neither restates snapshot facts. ``undetermined`` (confidence ``low``) means the
snapshots cannot tell, never a guess. Rules: docs/comparison/comparison-v1.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, Field, WithJsonSchema

from xraymyrepo.cim import (
    Confidence,
    EdgeChangeKind,
    EdgeKind,
    EdgeRef,
    Incomparability,
    MatchMethod,
    NodeChangeKind,
    SnapshotStatus,
)

from .catalog import SnapshotSummary
from .common import ApiModel, SnapshotNodeKind


def _listed(change: EdgeChangeKind) -> EdgeChangeKind:
    if change is EdgeChangeKind.UNCHANGED:
        raise ValueError("unchanged edges are counted, not listed: see the comparison's summary")
    return change


ListedEdgeChangeKind = Annotated[
    EdgeChangeKind,
    AfterValidator(_listed),
    WithJsonSchema(
        {
            "type": "string",
            "enum": [k.value for k in EdgeChangeKind if k is not EdgeChangeKind.UNCHANGED],
            "title": "ListedEdgeChangeKind",
        }
    ),
]
"""An ``EdgeChangeKind`` that edge changes can have: never ``unchanged`` (unchanged
edges are only counted)."""


class ComparisonRunSummary(ApiModel):
    id: int
    from_snapshot_id: int
    to_snapshot_id: int
    algorithm: str
    algorithm_version: str
    params_hash: str
    incomparable: list[Incomparability] = Field(
        description="How the two snapshot identities differ; empty when fully comparable. "
        "When not empty, no change has high confidence."
    )
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


class ChangeCounts(ApiModel):
    """Changes by kind; every kind is present, zeros included."""

    nodes: dict[NodeChangeKind, int]
    edges: dict[EdgeChangeKind, int]
    """``unchanged`` edges are counted here only; they are not listed as edge changes."""


class ComparisonDetail(ComparisonRunSummary):
    from_snapshot: SnapshotSummary
    to_snapshot: SnapshotSummary
    params: dict[str, Any]
    counts: ChangeCounts


class NodeChangeView(ApiModel):
    key: str
    """The node's key in ``to``, else in ``from``: unique within a comparison."""
    change: NodeChangeKind
    kind: SnapshotNodeKind
    """The node's kind in ``to``, else in ``from``."""
    from_key: str | None
    to_key: str | None
    match_method: MatchMethod | None
    """``same_key`` or ``same_content_hash`` (moved); null when only one side exists."""
    confidence: Confidence
    move_ambiguous: bool
    """An added or removed file whose content is on the other side, but not uniquely."""


class EdgeChangeView(ApiModel):
    change: ListedEdgeChangeKind
    kind: EdgeKind
    from_ref: EdgeRef | None
    """The edge in ``from``, in ``from`` keys."""
    to_ref: EdgeRef | None
    """The edge in ``to``, in ``to`` keys."""
    confidence: Confidence
