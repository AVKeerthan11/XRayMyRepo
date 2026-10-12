"""Change impact: what is affected if some nodes change (computed on request)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from xraymyrepo.cim import Confidence, EdgeRef, ExtractionStatus, UnresolvedReason

from .common import ApiModel, NodeBrief

ImpactCategory = Literal["file", "test", "endpoint"]


class ImpactItem(ApiModel):
    node: NodeBrief
    category: ImpactCategory
    distance: int
    """Hops from a seed; 0 for the changed files themselves. An endpoint has its
    handler's distance."""
    confidence: Confidence
    """``high`` when every edge of ``via`` is resolved or observed, ``medium`` when one
    is heuristic."""
    via: list[EdgeRef]
    """A shortest path of real edges from a seed."""


class ImpactSummary(ApiModel):
    max_depth: int
    affected_files: int
    """Files and tests, including the changed files at distance 0."""
    affected_tests: int
    affected_endpoints: int
    by_distance: dict[int, int]
    by_confidence: dict[Confidence, int]
    truncated: bool
    """The node bound stopped the traversal: more nodes may be affected."""
    beyond_depth: bool
    """Dependents exist beyond ``max_depth``."""
    blind_spot_files: int
    """Files whose dependencies cannot be fully seen (see ``impact/blind-spots``): they
    may depend on the seeds without it showing here."""


class ImpactReport(ApiModel):
    seeds: list[NodeBrief]
    summary: ImpactSummary
    items: list[ImpactItem]
    next_cursor: str | None = Field(
        description="Opaque; pass as `cursor` (with the same parameters) for the next page."
    )


class BlindSpotView(ApiModel):
    node: NodeBrief
    extraction_status: ExtractionStatus | None
    unresolved_imports: dict[UnresolvedReason, int]
    """Unresolved ``dynamic`` / ``not_found`` imports, by reason."""


class ImpactGroup(ApiModel):
    key: str
    label: str | None
    affected_files: int
    """Affected files in this group and its descendants."""
    min_distance: int


class ImpactGroupReport(ApiModel):
    lens: str
    parent_key: str | None
    affected_files: int
    """All affected files of the impact (whatever their group)."""
    direct_files: int
    """Affected files assigned to ``parent_key`` itself rather than to a child group."""
    unassigned_files: int
    """Affected files not under ``parent_key`` in this lens (outside it, or unassigned)."""
    items: list[ImpactGroup]
    next_cursor: str | None
