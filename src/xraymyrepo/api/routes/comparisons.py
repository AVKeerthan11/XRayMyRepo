"""Snapshot comparisons: what changed between two snapshots of one repository (read-only).

Comparisons are computed and stored outside the API
(``xraymyrepo.orchestration.compare_snapshots``); these routes only read them. Node
changes are listed in key order, edge changes in canonical order (kind, source key,
target key). Unchanged edges are only counted (``/summary``), never listed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import TypeAdapter

from xraymyrepo.cim import EdgeKind, NodeChangeKind
from xraymyrepo.persistence import comparison_queries as cq
from xraymyrepo.persistence import queries

from .. import cursors, mapping
from ..db import Conn
from ..deps import DEFAULT_LIMIT, CompleteComparison, CompleteSnapshot, Cursor, Limit
from ..errors import responses
from ..models import (
    ChangeCounts,
    ComparisonDetail,
    ComparisonRunSummary,
    EdgeChangeView,
    NodeChangeView,
    Page,
)
from ..models.common import SnapshotNodeKind
from ..models.comparison import ListedEdgeChangeKind

router = APIRouter(tags=["comparison"])

COMPARISON = "/comparisons/{comparison_id}"
_PROBLEMS = {"conflict": "Comparison not complete"}

_ID_CURSOR: TypeAdapter[int] = TypeAdapter(int)
_KEY_CURSOR: TypeAdapter[str] = TypeAdapter(str)


@router.get(
    "/snapshots/{snapshot_id}/comparisons",
    summary="Comparisons with the snapshot on either side, newest first",
    responses=responses(400, 404, 409),
)
def list_comparisons(
    conn: Conn, snapshot: CompleteSnapshot, limit: Limit = DEFAULT_LIMIT, cursor: Cursor = None
) -> Page[ComparisonRunSummary]:
    """Any status; a comparison is listed under both of its snapshots."""
    rows = cq.list_comparisons(
        conn, snapshot.id, after=cursors.decode(cursor, _ID_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda r: r.id)
    return Page(items=[mapping.comparison_summary(r) for r in items], next_cursor=next_cursor)


@router.get(
    COMPARISON,
    summary="A comparison: both snapshots, parameters, comparability and change counts",
    responses=responses(404, 409, **_PROBLEMS),
)
def get_comparison(conn: Conn, comparison: CompleteComparison) -> ComparisonDetail:
    before = queries.get_snapshot(conn, comparison.from_snapshot_id)
    after = queries.get_snapshot(conn, comparison.to_snapshot_id)
    assert before is not None and after is not None  # comparisons cascade with both
    return ComparisonDetail(
        **dict(mapping.comparison_summary(comparison)),
        from_snapshot=mapping.snapshot_summary(before),
        to_snapshot=mapping.snapshot_summary(after),
        params=cq.comparison_params(conn, comparison.id),
        counts=mapping.change_counts(cq.comparison_summary(conn, comparison.id)),
    )


@router.get(
    f"{COMPARISON}/summary",
    summary="Change counts by kind, unchanged edges included",
    responses=responses(404, 409, **_PROBLEMS),
)
def get_summary(conn: Conn, comparison: CompleteComparison) -> ChangeCounts:
    return mapping.change_counts(cq.comparison_summary(conn, comparison.id))


@router.get(
    f"{COMPARISON}/node-changes",
    summary="Node changes in key order, optionally by change and node kind",
    responses=responses(400, 404, 409, **_PROBLEMS),
)
def list_node_changes(
    conn: Conn,
    comparison: CompleteComparison,
    changes: Annotated[
        list[NodeChangeKind] | None, Query(alias="change", description="Repeatable filter")
    ] = None,
    kinds: Annotated[
        list[SnapshotNodeKind] | None, Query(alias="kind", description="Repeatable filter")
    ] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[NodeChangeView]:
    """Every node of both snapshots has exactly one change, ``unchanged`` included."""
    rows = cq.list_node_changes(
        conn,
        comparison.id,
        changes=changes or (),
        kinds=kinds or (),
        after=cursors.decode(cursor, _KEY_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, lambda n: n.key)
    return Page(items=[mapping.node_change_view(n) for n in items], next_cursor=next_cursor)


@router.get(
    f"{COMPARISON}/edge-changes",
    summary="Changed and undetermined edges in canonical order, optionally by change and kind",
    responses=responses(400, 404, 409, **_PROBLEMS),
)
def list_edge_changes(
    conn: Conn,
    comparison: CompleteComparison,
    changes: Annotated[
        list[ListedEdgeChangeKind] | None,
        Query(alias="change", description="Repeatable filter; not `unchanged`"),
    ] = None,
    kinds: Annotated[list[EdgeKind] | None, Query(alias="kind", description="Repeatable")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[EdgeChangeView]:
    """Ordered by edge kind, source key and target key (of the edge in ``to``, else in
    ``from``)."""
    rows = cq.list_edge_changes(
        conn,
        comparison.id,
        changes=changes or (),
        kinds=kinds or (),
        after=cursors.decode(cursor, _ID_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, lambda item: item[0])
    return Page(items=[mapping.edge_change_view(e) for _, e in items], next_cursor=next_cursor)
