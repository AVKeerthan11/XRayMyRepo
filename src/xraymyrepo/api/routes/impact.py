"""Change impact (computed on request from the cached snapshot graph; read-only).

The traversal itself is ``xraymyrepo.insights.impact``; this module validates the
seeds, pages the result and, for an architecture run, maps affected files onto a lens.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import TypeAdapter

from xraymyrepo.cim import Confidence, NodeKind
from xraymyrepo.graph import DependencyGraph
from xraymyrepo.insights.impact import ImpactParams, ImpactResult, impact
from xraymyrepo.insights.impact import blind_spots as find_blind_spots
from xraymyrepo.persistence import derivation_queries as dq

from .. import cursors
from ..db import Conn
from ..deps import (
    DEFAULT_LIMIT,
    CompleteRun,
    CompleteSnapshot,
    Cursor,
    Graphs,
    Limit,
    checked_key,
)
from ..errors import ApiError, not_found, responses
from ..graphs import snapshot_graph
from ..models import NodeBrief, Page
from ..models.impact import (
    BlindSpotView,
    ImpactCategory,
    ImpactGroup,
    ImpactGroupReport,
    ImpactItem,
    ImpactReport,
    ImpactSummary,
)
from .derivations import require_group, require_lens

router = APIRouter(tags=["impact"])

MAX_SEEDS = 50
Seeds = Annotated[
    list[str],
    Query(alias="node", description=f"Changed node key; repeatable (at most {MAX_SEEDS})"),
]
Depth = Annotated[int, Query(ge=0, le=10, description="Maximum hops from a seed")]

_ITEM_CURSOR: TypeAdapter[tuple[int, str]] = TypeAdapter(tuple[int, str])
_KEY_CURSOR: TypeAdapter[str] = TypeAdapter(str)


def _brief(graph: DependencyGraph, key: str) -> NodeBrief:
    node = graph.nodes[key]
    return NodeBrief(key=node.key, kind=node.kind, name=node.name)


def compute(graph: DependencyGraph, seeds: list[str], depth: int) -> ImpactResult:
    """Validate the seeds against the graph and traverse (no database access)."""
    unique = sorted(set(seeds))
    if len(unique) > MAX_SEEDS:
        raise ApiError(
            422,
            "invalid_parameters",
            "Invalid parameters",
            f"At most {MAX_SEEDS} distinct 'node' seeds.",
        )
    for key in unique:
        checked_key(key, "node")
        node = graph.nodes.get(key)
        if node is None:
            raise not_found("node_not_found", f"No node {key!r} in this snapshot.")
        if node.kind is NodeKind.REPOSITORY:
            raise ApiError(
                422,
                "invalid_parameters",
                "Invalid parameters",
                "The repository root cannot be a seed; name what changed.",
            )
    return impact(graph, unique, ImpactParams(max_depth=depth, max_seeds=MAX_SEEDS))


@router.get(
    "/snapshots/{snapshot_id}/impact",
    summary="What is affected if these nodes change",
    responses=responses(400, 404, 409),
)
def snapshot_impact(
    conn: Conn,
    snapshot: CompleteSnapshot,
    cache: Graphs,
    node: Seeds,
    depth: Depth = 3,
    category: Annotated[ImpactCategory | None, Query(description="Only this category")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> ImpactReport:
    """Affected files (``file``/``test``) and ``endpoint``s by (distance, key), each with a
    shortest witness path and a confidence. The summary covers the whole result; blind
    spots (files whose dependencies cannot be fully seen) are counted, never added."""
    graph = snapshot_graph(conn, cache, snapshot.id)
    result = compute(graph, node, depth)
    items = [i for i in result.items if category is None or i.category == category]
    after = cursors.decode(cursor, _ITEM_CURSOR)
    if after is not None:
        items = [i for i in items if (i.distance, i.key) > after]
    page, next_cursor = cursors.page(items, limit, lambda i: (i.distance, i.key))
    every = result.items
    by_distance: dict[int, int] = {}
    by_confidence: dict[Confidence, int] = {}
    for item in every:
        by_distance[item.distance] = by_distance.get(item.distance, 0) + 1
        by_confidence[item.confidence] = by_confidence.get(item.confidence, 0) + 1
    return ImpactReport(
        seeds=[_brief(graph, key) for key in result.seeds],
        summary=ImpactSummary(
            max_depth=result.max_depth,
            affected_files=sum(i.category in ("file", "test") for i in every),
            affected_tests=sum(i.category == "test" for i in every),
            affected_endpoints=sum(i.category == "endpoint" for i in every),
            by_distance=by_distance,
            by_confidence=by_confidence,
            truncated=result.truncated,
            beyond_depth=result.beyond_depth,
            blind_spot_files=len(result.blind_spots),
        ),
        items=[
            ImpactItem(
                node=_brief(graph, i.key),
                category=i.category,
                distance=i.distance,
                confidence=i.confidence,
                via=list(i.via),
            )
            for i in page
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/snapshots/{snapshot_id}/impact/blind-spots",
    summary="Files whose dependencies the snapshot cannot fully show",
    responses=responses(400, 404, 409),
)
def blind_spots(
    conn: Conn,
    snapshot: CompleteSnapshot,
    cache: Graphs,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[BlindSpotView]:
    """Files with unresolved ``dynamic`` or ``not_found`` imports, or whose extraction
    ``failed`` or was ``partial``: they may depend on anything without it showing in
    an impact result. Independent of seeds; in key order."""
    graph = snapshot_graph(conn, cache, snapshot.id)
    spots = list(find_blind_spots(graph))
    after = cursors.decode(cursor, _KEY_CURSOR)
    if after is not None:
        spots = [s for s in spots if s.file_key > after]
    page, next_cursor = cursors.page(spots, limit, lambda s: s.file_key)
    return Page(
        items=[
            BlindSpotView(
                node=_brief(graph, s.file_key),
                extraction_status=s.extraction_status,
                unresolved_imports=s.unresolved_imports,
            )
            for s in page
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/derivations/{run_id}/impact/groups",
    summary="Affected files per group of an architecture lens (one level)",
    responses=responses(400, 404, 409, conflict="Derivation run not complete"),
)
def impact_groups(
    conn: Conn,
    run: CompleteRun,
    cache: Graphs,
    node: Seeds,
    lens: Annotated[str, Query(description="e.g. `components` or `directory`")],
    parent: Annotated[str | None, Query(description="Group to break down; omit: top level")] = None,
    depth: Depth = 3,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> ImpactGroupReport:
    """The impact on the run's snapshot, counted per child group of ``parent`` (or per
    top-level group). Counts include descendant groups; ``min_distance`` is the closest
    affected file. Groups without affected files are omitted."""
    if parent is None:
        require_lens(conn, run.id, lens)
    else:
        group = require_group(conn, run.id, parent, "parent")
        if group.lens != lens:
            raise not_found("group_not_found", f"No group {parent!r} in lens {lens!r}.")
    graph = snapshot_graph(conn, cache, run.snapshot_id)
    result = compute(graph, node, depth)
    files = {i.key: i.distance for i in result.items if i.category in ("file", "test")}
    member_of = dq.memberships(conn, run.id, lens, sorted(files))
    groups = dq.lens_groups(conn, run.id, lens)
    counts: dict[str, list[int]] = {}
    direct = unassigned = 0
    for file_key, distance in files.items():
        chain: list[str] = []
        current: str | None = member_of.get(file_key)
        while current is not None:
            chain.append(current)
            current = groups[current].parent_key
        chain.reverse()  # root first
        if parent is None:
            child = chain[0] if chain else None
        elif parent in chain:
            position = chain.index(parent)
            child = chain[position + 1] if position + 1 < len(chain) else None
            if child is None:
                direct += 1
                continue
        else:
            child = None
        if child is None:
            unassigned += 1
            continue
        counts.setdefault(child, []).append(distance)
    entries = sorted(counts)
    after = cursors.decode(cursor, _KEY_CURSOR)
    if after is not None:
        entries = [k for k in entries if k > after]
    page, next_cursor = cursors.page(entries, limit, lambda k: k)
    return ImpactGroupReport(
        lens=lens,
        parent_key=parent,
        affected_files=len(files),
        direct_files=direct,
        unassigned_files=unassigned,
        items=[
            ImpactGroup(
                key=k,
                label=groups[k].label,
                affected_files=len(counts[k]),
                min_distance=min(counts[k]),
            )
            for k in page
        ],
        next_cursor=next_cursor,
    )
