"""Relationships of a node (one hop, paged) and single edges with their evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import TypeAdapter

from xraymyrepo.cim import EdgeKind, EdgeRef
from xraymyrepo.persistence import queries

from .. import cursors, mapping
from ..db import Conn
from ..deps import DEFAULT_LIMIT, CompleteSnapshot, Cursor, Limit, checked_key
from ..errors import not_found, responses
from ..models import EdgeDetail, Page, Relationship
from .nodes import require_node

router = APIRouter(tags=["relationships"])

_EDGE_CURSOR: TypeAdapter[queries.EdgeSortKey] = TypeAdapter(queries.EdgeSortKey)
_DIRECTIONS = {"out": queries.Direction.OUT, "in": queries.Direction.IN, "both": None}


@router.get(
    "/snapshots/{snapshot_id}/relationships",
    summary="Stored edges that start or end at a node",
    responses=responses(400, 404, 409),
)
def relationships(
    conn: Conn,
    snapshot: CompleteSnapshot,
    node: Annotated[str, Query(description="CIM node key")],
    direction: Literal["out", "in", "both"] = "both",
    kinds: Annotated[list[EdgeKind] | None, Query(alias="kind", description="Repeatable")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[Relationship]:
    """Outgoing edges first, then incoming, each by kind and peer key. Every item keeps
    the edge's stored direction; ``direction`` only says which end ``node`` is."""
    require_node(conn, snapshot.id, node, "node")
    rows = queries.get_edges(
        conn,
        snapshot.id,
        node,
        direction=_DIRECTIONS[direction],
        kinds=kinds or (),
        after=cursors.decode(cursor, _EDGE_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, queries.edge_sort_key)
    return Page(items=[mapping.relationship(i) for i in items], next_cursor=next_cursor)


@router.get(
    "/snapshots/{snapshot_id}/edges/by-ref",
    summary="One edge, identified by kind, source key and target key, with its evidence",
    responses=responses(404, 409),
)
def get_edge(
    conn: Conn,
    snapshot: CompleteSnapshot,
    kind: EdgeKind,
    source: Annotated[str, Query(description="Source node key")],
    target: Annotated[str, Query(description="Target node key")],
) -> EdgeDetail:
    ref = EdgeRef.of(kind, checked_key(source, "source"), checked_key(target, "target"))
    edge = queries.get_edge(conn, snapshot.id, ref)
    if edge is None:
        raise not_found(
            "edge_not_found", f"No {kind} edge {source!r} -> {target!r} in snapshot {snapshot.id}."
        )
    nodes = [queries.get_node(conn, snapshot.id, k) for k in (source, target)]
    ends = mapping.summaries(conn, snapshot.id, [n for n in nodes if n is not None])
    by_key = {s.key: s for s in ends}
    return mapping.edge_detail(edge, by_key[source], by_key[target])
