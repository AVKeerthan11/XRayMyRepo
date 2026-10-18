"""Store snapshot comparisons (migration 0004) and load them back.

A ``SnapshotComparison`` is computed elsewhere (``xraymyrepo.insights.comparison``) from
two complete snapshots of one repository; this module only maps it onto the database
and back. Like derivations:

* ``persist_comparison`` inserts the comparison as ``pending``, bulk-loads its node and
  edge changes and marks it ``complete`` with its summary, in one transaction; any
  failure leaves nothing behind.
* A comparison's identity is ``(from, to, algorithm, algorithm_version, params_hash)``;
  persisting an identity that exists raises ``ComparisonExistsError``.
* Complete comparisons and their rows are immutable
  (``snapshot_comparison_guard_immutable``, ``comparison_child_guard_immutable``).
  Completing checks the rows against both snapshots and the summary
  (``snapshot_comparison_check_complete``), so a stored comparison covers every node and
  accounts for every edge.
* Rows store database ids; keys are resolved on load by joins. Unchanged edges are not
  rows: their count is the summary's ``edges.unchanged``.
* Round trip: ``load_comparison(persist_comparison(...)) == comparison.canonical()``.

The parameters that produced the comparison are stored with it (``params``); they must
hash to the run's ``params_hash`` (sha256 of their canonical JSON).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import psycopg

from xraymyrepo.cim import (
    ComparisonSummary,
    EdgeChange,
    EdgeRef,
    SnapshotComparison,
    SnapshotStatus,
)

from . import _comparison_rows as R
from . import queries
from .derivations import StoredRun, params_hash
from .snapshots import (
    Connection,
    PersistenceError,
    SnapshotNotCompleteError,
    SnapshotNotFoundError,
)

EdgeKey = tuple[str, str, str]
"""(kind, source key, target key) of an edge in its own snapshot."""


class ComparisonExistsError(PersistenceError):
    """A comparison with the same identity is stored; a comparison is never replaced."""

    def __init__(self, comparison_id: int, status: SnapshotStatus) -> None:
        super().__init__(
            f"snapshot comparison {comparison_id} with this identity already exists ({status})"
        )
        self.comparison_id = comparison_id
        self.status = status


class ComparisonNotFoundError(PersistenceError):
    pass


class ComparisonNotCompleteError(PersistenceError):
    pass


# ---------------------------------------------------------------------------
# Writing


def persist_comparison(
    conn: Connection,
    from_snapshot_id: int,
    to_snapshot_id: int,
    comparison: SnapshotComparison,
    params: Mapping[str, Any],
) -> int:
    """Store ``comparison`` of snapshots ``from_snapshot_id`` -> ``to_snapshot_id`` as a
    complete comparison; returns its id.

    One transaction (a savepoint inside a caller's transaction).
    """
    run = comparison.run
    if run.status is not SnapshotStatus.COMPLETE:
        raise PersistenceError(f"only complete comparisons are stored, not {run.status}")
    if params_hash(params) != run.params_hash:
        raise PersistenceError("params do not hash to the comparison's params_hash")
    with conn.transaction(), conn.cursor() as cur:
        for side, snapshot_id, identity in (
            ("from", from_snapshot_id, run.from_snapshot),
            ("to", to_snapshot_id, run.to_snapshot),
        ):
            snapshot = queries.get_snapshot(conn, snapshot_id)
            if snapshot is None:
                raise SnapshotNotFoundError(f"snapshot {snapshot_id} does not exist")
            if snapshot.status is not SnapshotStatus.COMPLETE:
                raise SnapshotNotCompleteError(f"snapshot {snapshot_id} is {snapshot.status}")
            if snapshot.identity != identity:
                raise PersistenceError(f"snapshot {snapshot_id} is not the comparison's {side}")

        row = cur.execute(
            "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
            "algorithm_version, params_hash, params, incomparable) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT snapshot_comparison_identity_key DO NOTHING RETURNING id",
            (from_snapshot_id, to_snapshot_id, run.algorithm, run.algorithm_version,
             run.params_hash, json.dumps(params, sort_keys=True, ensure_ascii=False),
             json.dumps([reason.value for reason in run.incomparable])),
        ).fetchone()  # fmt: skip
        if row is None:
            existing = find_comparison(conn, from_snapshot_id, to_snapshot_id, run.algorithm,
                                       run.algorithm_version, run.params_hash)  # fmt: skip
            assert existing is not None
            raise ComparisonExistsError(existing.id, existing.status)
        comparison_id = int(row[0])
        canonical = comparison.canonical()
        _write_rows(cur, comparison_id, from_snapshot_id, to_snapshot_id, canonical)
        cur.execute(
            "UPDATE snapshot_comparison SET status = 'complete', finished_at = now(), "
            "summary = %s WHERE id = %s AND status = 'pending'",
            (canonical.summary().model_dump_json(), comparison_id),
        )
        if cur.rowcount != 1:
            raise PersistenceError(f"snapshot comparison {comparison_id} is no longer pending")
    return comparison_id


def _write_rows(
    cur: psycopg.Cursor[Any], comparison_id: int, from_id: int, to_id: int, c: SnapshotComparison
) -> None:
    before = _node_ids(cur, from_id, [n.from_key for n in c.nodes if n.from_key is not None])
    after = _node_ids(cur, to_id, [n.to_key for n in c.nodes if n.to_key is not None])
    _copy(
        cur,
        "COPY node_change (comparison_id, from_snapshot_id, to_snapshot_id, key, change, kind, "
        "from_node_id, to_node_id, match_method, confidence, move_ambiguous) FROM STDIN",
        ((comparison_id, from_id, to_id, n.key, n.change.value, n.kind.value,
          None if n.from_key is None else before[n.from_key],
          None if n.to_key is None else after[n.to_key],
          None if n.match_method is None else n.match_method.value,
          n.confidence.value, n.move_ambiguous)
         for n in c.nodes),
    )  # fmt: skip

    from_edges = _edge_ids(cur, from_id, before, [e.from_ref for e in c.edges if e.from_ref])
    to_edges = _edge_ids(cur, to_id, after, [e.to_ref for e in c.edges if e.to_ref])

    def edge_id(edges: dict[EdgeKey, int], ref: EdgeRef | None) -> int | None:
        return None if ref is None else edges[_edge_key(ref)]

    def row(e: EdgeChange) -> tuple[Any, ...]:
        return (comparison_id, from_id, to_id, e.change.value, e.kind.value,
                edge_id(from_edges, e.from_ref), edge_id(to_edges, e.to_ref),
                e.ref.source_key, e.ref.target_key, e.confidence.value)  # fmt: skip

    _copy(
        cur,
        "COPY edge_change (comparison_id, from_snapshot_id, to_snapshot_id, change, kind, "
        "from_edge_id, to_edge_id, source_key, target_key, confidence) FROM STDIN",
        (row(e) for e in c.edges),
    )


def _node_ids(cur: psycopg.Cursor[Any], snapshot_id: int, keys: list[str]) -> dict[str, int]:
    found = {
        key: int(node_id)
        for key, node_id in cur.execute(
            "SELECT key, id FROM node WHERE snapshot_id = %s AND key = ANY(%s)",
            (snapshot_id, keys),
        )
    }
    missing = sorted(k for k in keys if k not in found)
    if missing:
        raise PersistenceError(
            f"the comparison cites nodes snapshot {snapshot_id} lacks, e.g. {missing[0]!r}"
        )
    return found


def _edge_ids(
    cur: psycopg.Cursor[Any], snapshot_id: int, node_ids: dict[str, int], refs: list[EdgeRef]
) -> dict[EdgeKey, int]:
    """Ids of the edges ``refs`` names, by (kind, source id, target id) (``edge_unique``)."""
    keys = {v: k for k, v in node_ids.items()}
    wanted: list[tuple[str, int, int]] = []
    for ref in refs:
        if ref.source_key not in node_ids or ref.target_key not in node_ids:
            raise PersistenceError(
                f"the comparison cites edges snapshot {snapshot_id} lacks, e.g. {_edge_key(ref)}"
            )
        wanted.append((ref.kind.value, node_ids[ref.source_key], node_ids[ref.target_key]))
    found = {
        (kind, keys[source], keys[target]): int(edge_id)
        for kind, source, target, edge_id in cur.execute(
            "SELECT e.kind, e.source_id, e.target_id, e.id FROM edge e "
            "JOIN unnest(%s::text[], %s::bigint[], %s::bigint[]) AS w (kind, source_id, target_id) "
            "ON e.kind = w.kind AND e.source_id = w.source_id AND e.target_id = w.target_id "
            "WHERE e.snapshot_id = %s",
            ([w[0] for w in wanted], [w[1] for w in wanted], [w[2] for w in wanted], snapshot_id),
        )
    }
    missing = sorted(k for k in map(_edge_key, refs) if k not in found)
    if missing:
        raise PersistenceError(
            f"the comparison cites edges snapshot {snapshot_id} lacks, e.g. {missing[0]}"
        )
    return found


def _edge_key(ref: EdgeRef) -> EdgeKey:
    return (ref.kind.value, ref.source_key, ref.target_key)


def _copy(cur: psycopg.Cursor[Any], statement: str, rows: Iterable[tuple[Any, ...]]) -> None:
    with cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


# ---------------------------------------------------------------------------
# Reading


def find_comparison(
    conn: Connection,
    from_snapshot_id: int,
    to_snapshot_id: int,
    algorithm: str,
    algorithm_version: str,
    params_hash: str,
) -> StoredRun | None:
    """The stored comparison with this identity, in any status."""
    row = conn.execute(
        "SELECT id, status FROM snapshot_comparison WHERE from_snapshot_id = %s "
        "AND to_snapshot_id = %s AND algorithm = %s AND algorithm_version = %s "
        "AND params_hash = %s",
        (from_snapshot_id, to_snapshot_id, algorithm, algorithm_version, params_hash),
    ).fetchone()
    return None if row is None else StoredRun(int(row[0]), SnapshotStatus(row[1]))


def load_comparison_summary(conn: Connection, comparison_id: int) -> ComparisonSummary:
    """The counts of a complete comparison by change kind (unchanged edges included),
    without loading its rows."""
    return ComparisonSummary.model_validate(_header(conn, comparison_id)[-1])


def load_comparison(conn: Connection, comparison_id: int) -> SnapshotComparison:
    """Load a complete comparison as a validated ``SnapshotComparison``, in canonical order.

    Five queries whatever the size: the comparison, its two snapshots, and one per
    table; ids are resolved to node keys and edge refs by joins.
    """
    with conn.transaction(), conn.cursor() as cur:
        from_id, to_id, algorithm, version, hash_, incomparable, summary = _header(
            conn, comparison_id
        )
        from_snapshot = queries.get_snapshot(conn, from_id)
        to_snapshot = queries.get_snapshot(conn, to_id)
        assert from_snapshot is not None and to_snapshot is not None  # cascades with both
        nodes = cur.execute(
            f"SELECT {R.NODE_CHANGE_COLUMNS}{R.NODE_CHANGE_FROM} WHERE r.comparison_id = %s",
            (comparison_id,),
        ).fetchall()
        edges = cur.execute(
            f"SELECT {R.EDGE_CHANGE_COLUMNS}{R.EDGE_CHANGE_FROM} WHERE r.comparison_id = %s",
            (comparison_id,),
        ).fetchall()

    comparison = SnapshotComparison.model_validate({
        "run": {"from_snapshot": from_snapshot.identity, "to_snapshot": to_snapshot.identity,
                "algorithm": algorithm, "algorithm_version": version, "params_hash": hash_,
                "incomparable": incomparable, "status": SnapshotStatus.COMPLETE},
        "nodes": [R.node_change_data(r) for r in nodes],
        "edges": [R.edge_change_data(r) for r in edges],
        "unchanged_edges": summary["edges"]["unchanged"],
    })  # fmt: skip
    # Completing checked the summary against the rows; a difference means the stored
    # comparison was altered outside persistence.
    if comparison.summary() != ComparisonSummary.model_validate(summary):
        raise PersistenceError(f"snapshot comparison {comparison_id}: summary and rows differ")
    return comparison.canonical()


def _header(conn: Connection, comparison_id: int) -> tuple[Any, ...]:
    """(from id, to id, algorithm, version, params hash, incomparable, summary) of a
    complete comparison."""
    row = conn.execute(
        "SELECT from_snapshot_id, to_snapshot_id, algorithm, algorithm_version, params_hash, "
        "incomparable, status, summary FROM snapshot_comparison WHERE id = %s",
        (comparison_id,),
    ).fetchone()
    if row is None:
        raise ComparisonNotFoundError(f"snapshot comparison {comparison_id} does not exist")
    *header, status, summary = row
    if status != SnapshotStatus.COMPLETE:
        raise ComparisonNotCompleteError(f"snapshot comparison {comparison_id} is {status}")
    return (*header, summary)
