"""Read queries over stored derivation runs, for callers that need part of a run.

The derivation-tier counterpart of ``queries``: focused questions (one zoom level of
a group tree, one group's dependencies, the layer of one file) answered with a
bounded number of indexed statements, by group and node *key*, never by database id.
Every function that takes a ``run_id`` assumes the caller has checked that the run
is complete (``get_run``). Paged functions are keyset-paged like ``queries``.

"Unclassified" is explicit: a file the layering considered carries the metric
``layer_considered``; considered without an inferred layer means unclassified, and
a file without the metric was outside the layering scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from xraymyrepo.cim import (
    Confidence,
    EdgeKind,
    Finding,
    Group,
    GroupEdge,
    InferredLayer,
    LensKind,
    NodeKind,
    SnapshotStatus,
)
from xraymyrepo.cim.nodes import Node

from . import _derivation_rows as R
from . import _rows
from .queries import NodeBrief
from .snapshots import Connection

LAYER_CONSIDERED = "layer_considered"
"""Metric marking a file the layering considered (see module docstring)."""

GroupEdgeSortKey = tuple[str, str, str]


# ---------------------------------------------------------------------------
# Records


@dataclass(frozen=True)
class RunRecord:
    id: int
    snapshot_id: int
    algorithm: str
    algorithm_version: str
    params_hash: str
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class LensRecord:
    name: str
    kind: LensKind
    groups: int
    members: int
    """Files assigned to a group of the lens."""


@dataclass(frozen=True)
class LayerCoverage:
    considered: int
    classified: int
    by_layer: dict[str, int]
    by_confidence: dict[Confidence, int]

    @property
    def unclassified(self) -> int:
        return self.considered - self.classified


@dataclass(frozen=True)
class GroupBrief:
    key: str
    label: str | None


@dataclass(frozen=True)
class LayerMix:
    """Layers of the considered files in a group and its descendants."""

    by_layer: dict[str, int]
    unclassified: int


def group_edge_sort_key(edge: GroupEdge) -> GroupEdgeSortKey:
    return (edge.source_group_key, edge.edge_kind.value, edge.target_group_key)


# ---------------------------------------------------------------------------
# Runs

_RUN_COLUMNS = (
    "id, snapshot_id, algorithm, algorithm_version, params_hash, status, failure_reason, "
    "created_at, finished_at"
)


def _run(row: Sequence[Any]) -> RunRecord:
    return RunRecord(int(row[0]), int(row[1]), row[2], row[3], row[4], SnapshotStatus(row[5]),
                     row[6], row[7], row[8])  # fmt: skip


def list_runs(
    conn: Connection, snapshot_id: int, *, after: int | None = None, limit: int
) -> list[RunRecord]:
    """Runs of one snapshot (any status), newest first."""
    rows = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM derivation_run WHERE snapshot_id = %s "
        "AND (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
        (snapshot_id, after, after, limit),
    ).fetchall()
    return [_run(r) for r in rows]


def get_run(conn: Connection, run_id: int) -> RunRecord | None:
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM derivation_run WHERE id = %s", (run_id,)
    ).fetchone()
    return None if row is None else _run(row)


def run_params(conn: Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT params FROM derivation_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None, f"derivation run {run_id} does not exist"
    params: dict[str, Any] = row[0]
    return params


def run_lenses(conn: Connection, run_id: int) -> list[LensRecord]:
    rows = conn.execute(
        "SELECT l.name, l.kind, "
        "(SELECT count(*) FROM derived_group g WHERE g.run_id = l.run_id AND g.lens = l.name), "
        "(SELECT count(*) FROM group_member m WHERE m.run_id = l.run_id AND m.lens = l.name) "
        "FROM lens l WHERE l.run_id = %s ORDER BY l.name",
        (run_id,),
    ).fetchall()
    return [LensRecord(r[0], LensKind(r[1]), int(r[2]), int(r[3])) for r in rows]


def layer_coverage(conn: Connection, run_id: int) -> LayerCoverage:
    considered = conn.execute(
        "SELECT count(*) FROM node_metric WHERE run_id = %s AND metric = %s",
        (run_id, LAYER_CONSIDERED),
    ).fetchone()
    assert considered is not None
    rows = conn.execute(
        "SELECT layer, confidence, count(*) FROM inferred_layer WHERE run_id = %s "
        "GROUP BY layer, confidence",
        (run_id,),
    ).fetchall()
    by_layer: dict[str, int] = {}
    by_confidence: dict[Confidence, int] = {}
    for layer, confidence, count in rows:
        by_layer[layer] = by_layer.get(layer, 0) + int(count)
        by_confidence[Confidence(confidence)] = by_confidence.get(Confidence(confidence), 0) + int(
            count
        )
    return LayerCoverage(
        considered=int(considered[0]),
        classified=sum(by_layer.values()),
        by_layer=dict(sorted(by_layer.items())),
        by_confidence={c: by_confidence[c] for c in Confidence if c in by_confidence},
    )


def finding_counts(conn: Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT rule, count(*) FROM finding WHERE run_id = %s GROUP BY rule ORDER BY rule",
        (run_id,),
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# Groups


def lens_exists(conn: Connection, run_id: int, lens: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lens WHERE run_id = %s AND name = %s", (run_id, lens)
    ).fetchone()
    return row is not None


def get_group(conn: Connection, run_id: int, key: str) -> Group | None:
    row = conn.execute(
        f"SELECT {R.GROUP_COLUMNS}{R.GROUP_FROM} WHERE g.run_id = %s AND g.key = %s",
        (run_id, key),
    ).fetchone()
    return None if row is None else R.group(row)


def group_children(
    conn: Connection,
    run_id: int,
    lens: str,
    parent_key: str | None,
    *,
    after: str | None = None,
    limit: int,
) -> list[Group]:
    """Child groups of ``parent_key`` (the lens's top-level groups for None), by key."""
    if parent_key is None:
        where = "g.lens = %(lens)s AND g.parent_id IS NULL"
    else:
        where = (
            "g.lens = %(lens)s AND g.parent_id = "
            "(SELECT id FROM derived_group WHERE run_id = %(run)s AND key = %(parent)s)"
        )
    rows = conn.execute(
        f"SELECT {R.GROUP_COLUMNS}{R.GROUP_FROM} WHERE g.run_id = %(run)s AND {where} "
        "AND (%(after)s::text IS NULL OR g.key > %(after)s) ORDER BY g.key LIMIT %(limit)s",
        {"run": run_id, "lens": lens, "parent": parent_key, "after": after, "limit": limit},
    ).fetchall()
    return [R.group(r) for r in rows]


def group_ancestors(conn: Connection, run_id: int, key: str) -> list[GroupBrief]:
    """Groups from the lens root down to the group's parent."""
    rows = conn.execute(
        "WITH RECURSIVE up AS ("
        " SELECT p.id, p.parent_id, p.key, p.label, p.depth FROM derived_group g"
        " JOIN derived_group p ON p.run_id = g.run_id AND p.id = g.parent_id"
        " WHERE g.run_id = %(run)s AND g.key = %(key)s"
        " UNION ALL"
        " SELECT p.id, p.parent_id, p.key, p.label, p.depth FROM up"
        " JOIN derived_group p ON p.run_id = %(run)s AND p.id = up.parent_id"
        ") SELECT key, label FROM up ORDER BY depth",
        {"run": run_id, "key": key},
    ).fetchall()
    return [GroupBrief(r[0], r[1]) for r in rows]


def group_counts(conn: Connection, run_id: int, keys: Sequence[str]) -> dict[str, tuple[int, int]]:
    """(child groups, directly assigned files) of several groups, in one query."""
    rows = conn.execute(
        "SELECT g.key, "
        "(SELECT count(*) FROM derived_group c WHERE c.run_id = g.run_id "
        " AND c.lens = g.lens AND c.parent_id = g.id), "
        "(SELECT count(*) FROM group_member m WHERE m.run_id = g.run_id AND m.group_id = g.id) "
        "FROM derived_group g WHERE g.run_id = %s AND g.key = ANY(%s)",
        (run_id, list(keys)),
    ).fetchall()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


def group_metrics(
    conn: Connection, run_id: int, keys: Sequence[str]
) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        "SELECT g.key, m.metric, m.value FROM node_metric m "
        "JOIN derived_group g ON g.run_id = m.run_id AND g.id = m.group_id "
        "WHERE m.run_id = %s AND g.key = ANY(%s) ORDER BY g.key, m.metric",
        (run_id, list(keys)),
    ).fetchall()
    metrics: dict[str, dict[str, float]] = {}
    for key, metric, value in rows:
        metrics.setdefault(key, {})[metric] = float(value)
    return metrics


def layer_mix(conn: Connection, run_id: int, keys: Sequence[str]) -> dict[str, LayerMix]:
    """Layers of the considered files in each group's subtree, in one query."""
    rows = conn.execute(
        "WITH RECURSIVE sub AS ("
        " SELECT key AS root, id FROM derived_group WHERE run_id = %(run)s AND key = ANY(%(keys)s)"
        " UNION ALL"
        " SELECT sub.root, g.id FROM sub"
        " JOIN derived_group g ON g.run_id = %(run)s AND g.parent_id = sub.id"
        ") SELECT sub.root, l.layer, count(*) FROM sub"
        " JOIN group_member m ON m.run_id = %(run)s AND m.group_id = sub.id"
        " JOIN node_metric c ON c.run_id = m.run_id AND c.node_id = m.node_id"
        "  AND c.metric = %(considered)s"
        " LEFT JOIN inferred_layer l ON l.run_id = m.run_id AND l.node_id = m.node_id"
        " GROUP BY sub.root, l.layer",
        {"run": run_id, "keys": list(keys), "considered": LAYER_CONSIDERED},
    ).fetchall()
    layers: dict[str, dict[str, int]] = {k: {} for k in keys}
    unclassified: dict[str, int] = dict.fromkeys(keys, 0)
    for root, layer, count in rows:
        if layer is None:
            unclassified[root] = int(count)
        else:
            layers[root][layer] = int(count)
    return {k: LayerMix(dict(sorted(layers[k].items())), unclassified[k]) for k in keys}


def group_members(
    conn: Connection, run_id: int, group_key: str, *, after: str | None = None, limit: int
) -> list[Node]:
    """Files assigned directly to the group, by key."""
    rows = conn.execute(
        f"SELECT {_rows.NODE_COLUMNS}{_rows.NODE_FROM} "
        "JOIN group_member m ON m.snapshot_id = n.snapshot_id AND m.node_id = n.id "
        "JOIN derived_group g ON g.run_id = m.run_id AND g.id = m.group_id "
        "WHERE g.run_id = %(run)s AND g.key = %(group)s "
        "AND (%(after)s::text IS NULL OR n.key > %(after)s) ORDER BY n.key LIMIT %(limit)s",
        {"run": run_id, "group": group_key, "after": after, "limit": limit},
    ).fetchall()
    return [_rows.node(r) for r in rows]


# ---------------------------------------------------------------------------
# Group edges


def group_edges(
    conn: Connection,
    run_id: int,
    *,
    lens: str | None = None,
    within: str | None = None,
    group: str | None = None,
    direction: Literal["out", "in"] | None = None,
    kinds: Sequence[EdgeKind] = (),
    after: GroupEdgeSortKey | None = None,
    limit: int,
) -> list[GroupEdge]:
    """One zoom level or one group's dependencies, by (source, kind, target) key.

    Exactly one of: ``within`` (edges between the child groups of that group),
    ``group`` (edges leaving and/or entering it, per ``direction``; both when None),
    or ``lens`` alone (edges between the lens's top-level groups).
    """
    params: dict[str, Any] = {"run": run_id, "limit": limit}
    if within is not None:
        params["within"] = within
        parent = "(SELECT id FROM derived_group WHERE run_id = %(run)s AND key = %(within)s)"
        where = f"src.parent_id = {parent} AND dst.parent_id = {parent}"
    elif group is not None:
        params["group"] = group
        sides = {"out": "src.key = %(group)s", "in": "dst.key = %(group)s"}
        where = sides[direction] if direction else "(src.key = %(group)s OR dst.key = %(group)s)"
    else:
        assert lens is not None, "one of lens, within or group is required"
        params["lens"] = lens
        where = "e.lens = %(lens)s AND src.parent_id IS NULL AND dst.parent_id IS NULL"
    if kinds:
        where += " AND e.edge_kind = ANY(%(kinds)s)"
        params["kinds"] = [k.value for k in kinds]
    if after is not None:
        where += " AND (src.key, e.edge_kind, dst.key) > (%(a_src)s, %(a_kind)s, %(a_dst)s)"
        params.update(a_src=after[0], a_kind=after[1], a_dst=after[2])
    rows = conn.execute(
        f"SELECT {R.GROUP_EDGE_COLUMNS}{R.GROUP_EDGE_FROM} WHERE e.run_id = %(run)s AND {where} "
        "ORDER BY src.key, e.edge_kind, dst.key LIMIT %(limit)s",
        params,
    ).fetchall()
    return [R.group_edge(r) for r in rows]


# ---------------------------------------------------------------------------
# Layers and findings


def get_layer(conn: Connection, run_id: int, node_key: str) -> InferredLayer | None:
    row = conn.execute(
        f"SELECT {R.LAYER_COLUMNS}{R.LAYER_FROM} WHERE l.run_id = %s AND ln.key = %s",
        (run_id, node_key),
    ).fetchone()
    return None if row is None else R.layer(row)


def is_considered(conn: Connection, run_id: int, node_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM node_metric m JOIN node n ON n.snapshot_id = m.snapshot_id "
        "AND n.id = m.node_id WHERE m.run_id = %s AND m.metric = %s AND n.key = %s",
        (run_id, LAYER_CONSIDERED, node_key),
    ).fetchone()
    return row is not None


def list_layers(
    conn: Connection,
    run_id: int,
    *,
    layer: str | None = None,
    after: str | None = None,
    limit: int,
) -> list[InferredLayer]:
    """Inferred layers, optionally of one layer, by node key."""
    rows = conn.execute(
        f"SELECT {R.LAYER_COLUMNS}{R.LAYER_FROM} WHERE l.run_id = %(run)s "
        "AND (%(layer)s::text IS NULL OR l.layer = %(layer)s) "
        "AND (%(after)s::text IS NULL OR ln.key > %(after)s) ORDER BY ln.key LIMIT %(limit)s",
        {"run": run_id, "layer": layer, "after": after, "limit": limit},
    ).fetchall()
    return [R.layer(r) for r in rows]


def list_unclassified(
    conn: Connection, run_id: int, *, after: str | None = None, limit: int
) -> list[NodeBrief]:
    """Files the layering considered but did not classify, by key."""
    rows = conn.execute(
        "SELECT n.key, n.kind, n.name FROM node_metric m "
        "JOIN node n ON n.snapshot_id = m.snapshot_id AND n.id = m.node_id "
        "WHERE m.run_id = %(run)s AND m.metric = %(considered)s "
        "AND NOT EXISTS (SELECT 1 FROM inferred_layer l WHERE l.run_id = m.run_id "
        "AND l.node_id = m.node_id) "
        "AND (%(after)s::text IS NULL OR n.key > %(after)s) ORDER BY n.key LIMIT %(limit)s",
        {"run": run_id, "considered": LAYER_CONSIDERED, "after": after, "limit": limit},
    ).fetchall()
    return [NodeBrief(r[0], NodeKind(r[1]), r[2]) for r in rows]


def list_findings(
    conn: Connection,
    run_id: int,
    *,
    rule: str | None = None,
    subject: str | None = None,
    after: int | None = None,
    limit: int,
) -> list[tuple[int, Finding]]:
    """(id, finding) in creation order, optionally of one rule and/or about one subject
    (a node or group key among the finding's ``subject_keys``)."""
    rows = conn.execute(
        f"SELECT {R.FINDING_COLUMNS}, f.id{R.FINDING_FROM} WHERE f.run_id = %(run)s "
        "AND (%(rule)s::text IS NULL OR f.rule = %(rule)s) "
        "AND (%(subject)s::text IS NULL OR f.subject_keys @> jsonb_build_array(%(subject)s::text)) "
        "AND (%(after)s::bigint IS NULL OR f.id > %(after)s) ORDER BY f.id LIMIT %(limit)s",
        {"run": run_id, "rule": rule, "subject": subject, "after": after, "limit": limit},
    ).fetchall()
    return [(int(r[7]), R.finding(r)) for r in rows]


# ---------------------------------------------------------------------------
# Metrics

MetricSortKey = tuple[float, str]


@dataclass(frozen=True)
class MetricRecord:
    subject_key: str
    """A node key or a group key."""
    metric: str
    value: float
    producer: str
    rule: str | None


def metric_sort_key(record: MetricRecord) -> MetricSortKey:
    return (record.value, record.subject_key)


def list_metrics(
    conn: Connection,
    run_id: int,
    metric: str,
    *,
    descending: bool = True,
    after: MetricSortKey | None = None,
    limit: int,
) -> list[MetricRecord]:
    """One metric's values over every subject (nodes and groups), ranked by value, ties
    broken by subject key ascending."""
    params: dict[str, Any] = {"run": run_id, "metric": metric, "limit": limit}
    keyset = ""
    if after is not None:
        params.update(value=after[0], key=after[1])
        op = "<" if descending else ">"
        keyset = (
            f" AND (m.value {op} %(value)s "
            "OR (m.value = %(value)s AND coalesce(n.key, g.key) > %(key)s))"
        )
    order = "DESC" if descending else "ASC"
    rows = conn.execute(
        "SELECT coalesce(n.key, g.key), m.metric, m.value, m.producer, m.rule FROM node_metric m "
        "LEFT JOIN node n ON n.snapshot_id = m.snapshot_id AND n.id = m.node_id "
        "LEFT JOIN derived_group g ON g.run_id = m.run_id AND g.id = m.group_id "
        f"WHERE m.run_id = %(run)s AND m.metric = %(metric)s{keyset} "
        f"ORDER BY m.value {order}, coalesce(n.key, g.key) LIMIT %(limit)s",
        params,
    ).fetchall()
    return [MetricRecord(r[0], r[1], float(r[2]), r[3], r[4]) for r in rows]


# ---------------------------------------------------------------------------
# Memberships (for mapping arbitrary files onto a lens)


def memberships(
    conn: Connection, run_id: int, lens: str, node_keys: Sequence[str]
) -> dict[str, str]:
    """node key -> group key in one lens, for the given files (unassigned ones omitted)."""
    rows = conn.execute(
        "SELECT n.key, g.key FROM group_member m "
        "JOIN node n ON n.snapshot_id = m.snapshot_id AND n.id = m.node_id "
        "JOIN derived_group g ON g.run_id = m.run_id AND g.id = m.group_id "
        "WHERE m.run_id = %s AND m.lens = %s AND n.key = ANY(%s)",
        (run_id, lens, list(node_keys)),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


@dataclass(frozen=True)
class LensGroup:
    key: str
    parent_key: str | None
    label: str | None


def lens_groups(conn: Connection, run_id: int, lens: str) -> dict[str, LensGroup]:
    """Every group of a lens, by key, in one query."""
    rows = conn.execute(
        "SELECT g.key, p.key, g.label FROM derived_group g "
        "LEFT JOIN derived_group p ON p.run_id = g.run_id AND p.id = g.parent_id "
        "WHERE g.run_id = %s AND g.lens = %s",
        (run_id, lens),
    ).fetchall()
    return {r[0]: LensGroup(r[0], r[1], r[2]) for r in rows}
