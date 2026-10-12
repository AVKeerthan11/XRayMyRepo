"""Architecture intelligence: derivation runs and what they derived (read-only).

Kept apart from the snapshot routes so derived conclusions can never be mistaken for
snapshot facts. Group keys (``group:<lens>:<local>``) contain ``:``, ``/`` and ``#``
and are passed as query parameters, like node keys.

Lazy loading: ``groups/children`` expands one group (or a lens's top level) at a
time; ``group-edges?within=X`` returns the dependencies between X's children, i.e.
one zoom level; ``group-edges?group=Y`` the dependencies of one group.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import TypeAdapter

from xraymyrepo.cim import EdgeKind, Group
from xraymyrepo.persistence import derivation_queries as dq
from xraymyrepo.persistence import queries

from .. import cursors, mapping
from ..db import Conn
from ..deps import (
    DEFAULT_LIMIT,
    CompleteRun,
    CompleteSnapshot,
    Cursor,
    Limit,
    checked_group_key,
    checked_key,
)
from ..errors import ApiError, not_found, responses
from ..models import (
    DerivationRunDetail,
    DerivationRunSummary,
    FindingView,
    GroupDetail,
    GroupEdgeView,
    GroupSummary,
    LayerAssignment,
    MetricView,
    NodeBrief,
    NodeLayer,
    NodeSummary,
    Page,
)

router = APIRouter(tags=["architecture"])

RUN = "/derivations/{run_id}"
_RUN_PROBLEMS = {"conflict": "Derivation run not complete"}

_ID_CURSOR: TypeAdapter[int] = TypeAdapter(int)
_KEY_CURSOR: TypeAdapter[str] = TypeAdapter(str)
_GROUP_EDGE_CURSOR: TypeAdapter[dq.GroupEdgeSortKey] = TypeAdapter(dq.GroupEdgeSortKey)
_METRIC_CURSOR: TypeAdapter[dq.MetricSortKey] = TypeAdapter(dq.MetricSortKey)

GroupKey = Annotated[str, Query(description="CIM group key, e.g. `group:components:/#app`")]


def require_group(conn: Conn, run_id: int, key: str, parameter: str) -> Group:
    group = dq.get_group(conn, run_id, checked_group_key(key, parameter))
    if group is None:
        raise not_found("group_not_found", f"No group {key!r} in derivation run {run_id}.")
    return group


def require_lens(conn: Conn, run_id: int, lens: str) -> None:
    if not dq.lens_exists(conn, run_id, lens):
        raise not_found("lens_not_found", f"No lens {lens!r} in derivation run {run_id}.")


# ---------------------------------------------------------------------------
# Runs


@router.get(
    "/snapshots/{snapshot_id}/derivations",
    summary="Derivation runs of a snapshot, newest first",
    responses=responses(400, 404, 409),
)
def list_runs(
    conn: Conn, snapshot: CompleteSnapshot, limit: Limit = DEFAULT_LIMIT, cursor: Cursor = None
) -> Page[DerivationRunSummary]:
    rows = dq.list_runs(
        conn, snapshot.id, after=cursors.decode(cursor, _ID_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda r: r.id)
    return Page(items=[mapping.run_summary(r) for r in items], next_cursor=next_cursor)


@router.get(
    RUN,
    summary="A derivation run: parameters, lenses, layer coverage, finding counts",
    responses=responses(404, 409, **_RUN_PROBLEMS),
)
def get_run(conn: Conn, run: CompleteRun) -> DerivationRunDetail:
    return DerivationRunDetail(
        **dict(mapping.run_summary(run)),
        params=dq.run_params(conn, run.id),
        lenses=[mapping.lens_summary(lens) for lens in dq.run_lenses(conn, run.id)],
        layer_coverage=mapping.layer_coverage(dq.layer_coverage(conn, run.id)),
        finding_counts=dq.finding_counts(conn, run.id),
    )


# ---------------------------------------------------------------------------
# Groups


@router.get(
    f"{RUN}/groups/children",
    summary="Expand one group of a lens, or list the lens's top-level groups",
    responses=responses(400, 404, 409, **_RUN_PROBLEMS),
)
def group_children(
    conn: Conn,
    run: CompleteRun,
    lens: Annotated[str, Query(description="Lens name, e.g. `components` or `directory`")],
    parent: Annotated[
        str | None, Query(description="Group to expand; omit for the top level")
    ] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[GroupSummary]:
    """Child groups in key order. Each carries counts, metrics and its subtree's layer
    mix, so a tree can be drawn without further requests."""
    if parent is None:
        require_lens(conn, run.id, lens)
    else:
        group = require_group(conn, run.id, parent, "parent")
        if group.lens != lens:
            raise not_found("group_not_found", f"No group {parent!r} in lens {lens!r}.")
    rows = dq.group_children(
        conn, run.id, lens, parent, after=cursors.decode(cursor, _KEY_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda g: g.key)
    return Page(items=mapping.group_summaries(conn, run.id, items), next_cursor=next_cursor)


@router.get(
    f"{RUN}/groups/by-key",
    summary="Inspect one group: provenance, evidence, metrics, layer mix, ancestors",
    responses=responses(404, 409, **_RUN_PROBLEMS),
)
def get_group(conn: Conn, run: CompleteRun, key: GroupKey) -> GroupDetail:
    group = require_group(conn, run.id, key, "key")
    summary = mapping.group_summaries(conn, run.id, [group])[0]
    return mapping.group_detail(summary, group, dq.group_ancestors(conn, run.id, key))


@router.get(
    f"{RUN}/groups/members",
    summary="Files assigned directly to a group",
    responses=responses(400, 404, 409, **_RUN_PROBLEMS),
)
def group_members(
    conn: Conn,
    run: CompleteRun,
    group: GroupKey,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[NodeSummary]:
    """In key order, as snapshot node summaries (inspect them with the snapshot routes).
    Files of descendant groups are listed under those groups."""
    require_group(conn, run.id, group, "group")
    rows = dq.group_members(
        conn, run.id, group, after=cursors.decode(cursor, _KEY_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda n: n.key)
    return Page(items=mapping.summaries(conn, run.snapshot_id, items), next_cursor=next_cursor)


@router.get(
    f"{RUN}/group-edges",
    summary="Rolled-up dependencies between groups: one zoom level or one group",
    responses=responses(400, 404, 409, **_RUN_PROBLEMS),
)
def group_edges(
    conn: Conn,
    run: CompleteRun,
    lens: Annotated[
        str | None, Query(description="Edges between the lens's top-level groups")
    ] = None,
    within: Annotated[str | None, Query(description="Edges between this group's children")] = None,
    group: Annotated[
        str | None, Query(description="Edges leaving and/or entering this group")
    ] = None,
    direction: Annotated[
        Literal["out", "in", "both"], Query(description="With `group` only")
    ] = "both",
    kinds: Annotated[list[EdgeKind] | None, Query(alias="kind", description="Repeatable")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[GroupEdgeView]:
    """Exactly one of ``lens``, ``within`` or ``group``. Ordered by source group key,
    edge kind and target group key."""
    if sum(p is not None for p in (lens, within, group)) != 1:
        raise ApiError(
            422,
            "invalid_parameters",
            "Invalid parameters",
            "Give exactly one of 'lens', 'within' or 'group'.",
        )
    if direction != "both" and group is None:
        raise ApiError(
            422,
            "invalid_parameters",
            "Invalid parameters",
            "'direction' applies only together with 'group'.",
        )
    if lens is not None:
        require_lens(conn, run.id, lens)
    else:
        key = within if within is not None else group
        assert key is not None
        require_group(conn, run.id, key, "within" if within is not None else "group")
    rows = dq.group_edges(
        conn,
        run.id,
        lens=lens,
        within=within,
        group=group,
        direction=None if direction == "both" else direction,
        kinds=kinds or (),
        after=cursors.decode(cursor, _GROUP_EDGE_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, dq.group_edge_sort_key)
    return Page(items=[mapping.group_edge_view(e) for e in items], next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Layers and findings


@router.get(
    f"{RUN}/layers",
    summary="Inferred layers, optionally of one layer, by node key",
    responses=responses(400, 409, **_RUN_PROBLEMS),
)
def list_layers(
    conn: Conn,
    run: CompleteRun,
    layer: Annotated[str | None, Query(description="e.g. `interface`")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[LayerAssignment]:
    rows = dq.list_layers(
        conn, run.id, layer=layer, after=cursors.decode(cursor, _KEY_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda x: x.node_key)
    return Page(items=[mapping.layer_assignment(x) for x in items], next_cursor=next_cursor)


@router.get(
    f"{RUN}/layers/by-node",
    summary="The layer of one file: classified, unclassified or out of scope",
    responses=responses(404, 409, **_RUN_PROBLEMS),
)
def node_layer(
    conn: Conn,
    run: CompleteRun,
    node: Annotated[str, Query(description="CIM node key")],
) -> NodeLayer:
    brief = queries.get_node_brief(conn, run.snapshot_id, checked_key(node, "node"))
    if brief is None:
        raise not_found("node_not_found", f"No node {node!r} in snapshot {run.snapshot_id}.")
    layer = dq.get_layer(conn, run.id, node)
    if layer is not None:
        status: Literal["classified", "unclassified", "out_of_scope"] = "classified"
    else:
        status = "unclassified" if dq.is_considered(conn, run.id, node) else "out_of_scope"
    return NodeLayer(
        node=mapping.node_brief(brief),
        status=status,
        assignment=None if layer is None else mapping.layer_assignment(layer),
    )


@router.get(
    f"{RUN}/layers/unclassified",
    summary="Files the layering considered but could not classify",
    responses=responses(400, 409, **_RUN_PROBLEMS),
)
def list_unclassified(
    conn: Conn, run: CompleteRun, limit: Limit = DEFAULT_LIMIT, cursor: Cursor = None
) -> Page[NodeBrief]:
    rows = dq.list_unclassified(
        conn, run.id, after=cursors.decode(cursor, _KEY_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda b: b.key)
    return Page(items=[mapping.node_brief(b) for b in items], next_cursor=next_cursor)


def checked_subject_key(key: str) -> str:
    """A finding subject is a node key or a group key."""
    if key.startswith("group:"):
        return checked_group_key(key, "subject")
    return checked_key(key, "subject")


@router.get(
    f"{RUN}/findings",
    summary="Findings of a run, in creation order",
    responses=responses(400, 409, **_RUN_PROBLEMS),
)
def list_findings(
    conn: Conn,
    run: CompleteRun,
    rule: Annotated[str | None, Query(description="e.g. `health.import.cycle`")] = None,
    subject: Annotated[
        str | None, Query(description="Only findings about this node or group key")
    ] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[FindingView]:
    """Architecture runs report layer violations and group cycles; health runs report
    file cycles, hubs, unreferenced modules, unused declared dependencies (all
    ``inferred``) and coverage blind spots (``observed``)."""
    rows = dq.list_findings(
        conn,
        run.id,
        rule=rule,
        subject=None if subject is None else checked_subject_key(subject),
        after=cursors.decode(cursor, _ID_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, lambda item: item[0])
    return Page(items=[mapping.finding_view(i, f) for i, f in items], next_cursor=next_cursor)


@router.get(
    f"{RUN}/metrics",
    summary="One metric over every subject of a run, ranked by value",
    responses=responses(400, 409, **_RUN_PROBLEMS),
)
def list_metrics(
    conn: Conn,
    run: CompleteRun,
    metric: Annotated[str, Query(description="e.g. `fan_in` (health) or `instability`")],
    order: Annotated[Literal["desc", "asc"], Query(description="By value")] = "desc",
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[MetricView]:
    """Subjects are node keys (file metrics) or group keys (group metrics); ties are
    ordered by subject key."""
    rows = dq.list_metrics(
        conn,
        run.id,
        metric,
        descending=order == "desc",
        after=cursors.decode(cursor, _METRIC_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, dq.metric_sort_key)
    return Page(items=[mapping.metric_view(m) for m in items], next_cursor=next_cursor)
