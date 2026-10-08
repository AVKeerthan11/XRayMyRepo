"""Repositories and snapshots."""

from __future__ import annotations

from datetime import datetime

from xraymyrepo.cim import (
    CoverageDeclaration,
    EdgeKind,
    IssueSeverity,
    Producer,
    RepositoryRef,
    SnapshotStatus,
    UnresolvedReason,
)
from xraymyrepo.cim.snapshot import AnalysisConfig

from .common import ApiModel, SnapshotNodeKind
from .nodes import NodeSummary


class SnapshotSummary(ApiModel):
    id: int
    repository: RepositoryRef
    commit_sha: str
    extractor_version: str
    config_hash: str
    cim_schema_version: str
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


class Repository(ApiModel):
    host: str
    owner: str
    name: str
    slug: str
    snapshot_count: int
    """Complete snapshots."""
    latest_snapshot: SnapshotSummary | None
    """The most recently created complete snapshot (not necessarily the newest commit)."""


class SnapshotStats(ApiModel):
    nodes_by_kind: dict[SnapshotNodeKind, int]
    edges_by_kind: dict[EdgeKind, int]
    unresolved_by_reason: dict[UnresolvedReason, int]
    issues_by_severity: dict[IssueSeverity, int]


class SnapshotDetail(SnapshotSummary):
    config: AnalysisConfig
    producers: list[Producer]
    coverage: list[CoverageDeclaration]
    root: NodeSummary
    stats: SnapshotStats
