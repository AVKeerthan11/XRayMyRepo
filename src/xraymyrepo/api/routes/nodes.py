"""Tree expansion, node listing and inspection, search and diagnostics.

Node keys contain ``/``, ``#``, ``:`` and spaces, so they are passed as query
parameters (URL-encoded; ``#`` must be sent as ``%23``), never as path segments.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import TypeAdapter

from xraymyrepo.cim import REPOSITORY_KEY
from xraymyrepo.persistence import queries

from .. import cursors, mapping
from ..db import Conn
from ..deps import DEFAULT_LIMIT, CompleteSnapshot, Cursor, Limit, checked_key
from ..errors import not_found, responses
from ..models import Diagnostics, NodeDetail, NodeSummary, Page, SearchHit
from ..models.common import SnapshotNodeKind

router = APIRouter(tags=["nodes"])

Kinds = Annotated[
    list[SnapshotNodeKind] | None, Query(alias="kind", description="Repeatable filter")
]

_CHILD_CURSOR: TypeAdapter[queries.ChildSortKey] = TypeAdapter(queries.ChildSortKey)
_KEY_CURSOR: TypeAdapter[str] = TypeAdapter(str)
_SEARCH_CURSOR: TypeAdapter[queries.SearchSortKey] = TypeAdapter(queries.SearchSortKey)


def require_node(conn: Conn, snapshot_id: int, key: str, parameter: str) -> queries.NodeBrief:
    brief = queries.get_node_brief(conn, snapshot_id, checked_key(key, parameter))
    if brief is None:
        raise not_found("node_not_found", f"No node {key!r} in snapshot {snapshot_id}.")
    return brief


@router.get(
    "/snapshots/{snapshot_id}/tree/children",
    summary="Expand one node of the declaration tree",
    responses=responses(400, 404, 409),
)
def tree_children(
    conn: Conn,
    snapshot: CompleteSnapshot,
    parent: Annotated[str, Query(description="Key of the node to expand")] = REPOSITORY_KEY,
    kinds: Kinds = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[NodeSummary]:
    """Directories, then files (by key); inside a file, types and functions in source
    order, then endpoints. Each child carries ``child_count`` so it can show an
    expander without another request. Packages are not in the tree; list them with
    ``/nodes?kind=package``."""
    require_node(conn, snapshot.id, parent, "parent")
    rows = queries.get_children(
        conn,
        snapshot.id,
        parent,
        kinds=kinds or (),
        after=cursors.decode(cursor, _CHILD_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, queries.child_sort_key)
    return Page(items=mapping.summaries(conn, snapshot.id, items), next_cursor=next_cursor)


@router.get(
    "/snapshots/{snapshot_id}/nodes",
    summary="List nodes by kind and/or key prefix",
    responses=responses(400, 409),
)
def list_nodes(
    conn: Conn,
    snapshot: CompleteSnapshot,
    kinds: Kinds = None,
    prefix: Annotated[str | None, Query(description="Key prefix, e.g. `src/app/`")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[NodeSummary]:
    """In key (code point) order. Use it for nodes outside the tree: packages,
    external packages and endpoints."""
    rows = queries.list_nodes(
        conn,
        snapshot.id,
        kinds=kinds or (),
        prefix=prefix,
        after=cursors.decode(cursor, _KEY_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, lambda n: n.key)
    return Page(items=mapping.summaries(conn, snapshot.id, items), next_cursor=next_cursor)


@router.get(
    "/snapshots/{snapshot_id}/nodes/by-key",
    summary="Inspect one node",
    responses=responses(404, 409),
)
def get_node(
    conn: Conn,
    snapshot: CompleteSnapshot,
    key: Annotated[str, Query(description="CIM node key")],
) -> NodeDetail:
    node = queries.get_node(conn, snapshot.id, checked_key(key, "key"))
    if node is None:
        raise not_found("node_not_found", f"No node {key!r} in snapshot {snapshot.id}.")
    sid = snapshot.id
    return mapping.node_detail(
        node,
        child_count=queries.count_children(conn, sid, [key]).get(key, 0),
        classifications=queries.get_classifications(conn, sid, key),
        ancestors=queries.get_ancestors(conn, sid, key),
        edge_counts=queries.edge_counts(conn, sid, key),
        diagnostic_counts=queries.diagnostic_counts(conn, sid, key),
    )


@router.get(
    "/snapshots/{snapshot_id}/search",
    summary="Find nodes by name, key or path",
    responses=responses(400, 409),
)
def search(
    conn: Conn,
    snapshot: CompleteSnapshot,
    q: Annotated[str, Query(min_length=1, max_length=200, description="Text to find")],
    kinds: Kinds = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[SearchHit]:
    """Case-insensitive substring match. Exact names first, then name prefixes, then
    other name matches, then nodes whose key (path) contains the text."""
    rows = queries.search_nodes(
        conn,
        snapshot.id,
        q,
        kinds=kinds or (),
        after=cursors.decode(cursor, _SEARCH_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, queries.search_sort_key)
    nodes = mapping.summaries(conn, snapshot.id, [m.node for m in items])
    return Page(
        items=[SearchHit(node=n, matched=m.matched) for n, m in zip(nodes, items, strict=True)],
        next_cursor=next_cursor,
    )


@router.get(
    "/snapshots/{snapshot_id}/diagnostics",
    summary="What the extractors could not see around a node",
    responses=responses(404, 409),
)
def diagnostics(
    conn: Conn,
    snapshot: CompleteSnapshot,
    node: Annotated[str, Query(description="CIM node key")],
) -> Diagnostics:
    brief = require_node(conn, snapshot.id, node, "node")
    found = queries.get_diagnostics(conn, snapshot.id, node)
    return Diagnostics(
        node=mapping.node_brief(brief),
        unresolved_references=list(found.unresolved_references),
        extraction_issues=list(found.extraction_issues),
    )
