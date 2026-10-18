"""Read queries over stored snapshot comparisons, for callers that need part of one.

The comparison counterpart of ``derivation_queries``: one comparison's record, its
summary, and its node and edge changes a page at a time, with a bounded number of
indexed statements, returned by *key* (CIM records), never by database id. Every
function that takes a ``comparison_id`` assumes the caller has checked that the
comparison is complete (``get_comparison``). Paged functions are keyset-paged:

* node changes in key order (the node's key in ``to``, else in ``from``; unique per
  comparison), after a key;
* edge changes in canonical order (kind, source key, target key), which is their
  storage order (``persist_comparison`` writes them canonically), after a row id.

Unchanged edges are not rows: they are only counted (``comparison_summary``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xraymyrepo.cim import (
    ComparisonSummary,
    EdgeChange,
    EdgeChangeKind,
    EdgeKind,
    Incomparability,
    NodeChange,
    NodeChangeKind,
    NodeKind,
    SnapshotStatus,
)

from . import _comparison_rows as R
from .snapshots import Connection


@dataclass(frozen=True)
class ComparisonRecord:
    id: int
    from_snapshot_id: int
    to_snapshot_id: int
    algorithm: str
    algorithm_version: str
    params_hash: str
    incomparable: tuple[Incomparability, ...]
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


_COLUMNS = (
    "id, from_snapshot_id, to_snapshot_id, algorithm, algorithm_version, params_hash, "
    "incomparable, status, failure_reason, created_at, finished_at"
)


def _comparison(row: Sequence[Any]) -> ComparisonRecord:
    return ComparisonRecord(int(row[0]), int(row[1]), int(row[2]), row[3], row[4], row[5],
                            tuple(Incomparability(r) for r in row[6]), SnapshotStatus(row[7]),
                            row[8], row[9], row[10])  # fmt: skip


def get_comparison(conn: Connection, comparison_id: int) -> ComparisonRecord | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM snapshot_comparison WHERE id = %s", (comparison_id,)
    ).fetchone()
    return None if row is None else _comparison(row)


def list_comparisons(
    conn: Connection, snapshot_id: int, *, after: int | None = None, limit: int
) -> list[ComparisonRecord]:
    """Comparisons (any status) with the snapshot on either side, newest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM snapshot_comparison "
        "WHERE (from_snapshot_id = %(s)s OR to_snapshot_id = %(s)s) "
        "AND (%(after)s::bigint IS NULL OR id < %(after)s) ORDER BY id DESC LIMIT %(limit)s",
        {"s": snapshot_id, "after": after, "limit": limit},
    ).fetchall()
    return [_comparison(r) for r in rows]


def comparison_params(conn: Connection, comparison_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT params FROM snapshot_comparison WHERE id = %s", (comparison_id,)
    ).fetchone()
    assert row is not None, f"snapshot comparison {comparison_id} does not exist"
    params: dict[str, Any] = row[0]
    return params


def comparison_summary(conn: Connection, comparison_id: int) -> ComparisonSummary:
    """The stored counts by change kind, unchanged edges included (complete only)."""
    row = conn.execute(
        "SELECT summary FROM snapshot_comparison WHERE id = %s", (comparison_id,)
    ).fetchone()
    assert row is not None and row[0] is not None, f"comparison {comparison_id} is not complete"
    return ComparisonSummary.model_validate(row[0])


def list_node_changes(
    conn: Connection,
    comparison_id: int,
    *,
    changes: Sequence[NodeChangeKind] = (),
    kinds: Sequence[NodeKind] = (),
    after: str | None = None,
    limit: int,
) -> list[NodeChange]:
    """Node changes in key order, optionally of some change kinds and/or node kinds."""
    rows = conn.execute(
        f"SELECT {R.NODE_CHANGE_COLUMNS}{R.NODE_CHANGE_FROM} WHERE r.comparison_id = %(c)s "
        "AND (cardinality(%(changes)s::text[]) = 0 OR r.change = ANY(%(changes)s)) "
        "AND (cardinality(%(kinds)s::text[]) = 0 OR r.kind = ANY(%(kinds)s)) "
        "AND (%(after)s::text IS NULL OR r.key > %(after)s) ORDER BY r.key LIMIT %(limit)s",
        {"c": comparison_id, "changes": [c.value for c in changes],
         "kinds": [k.value for k in kinds], "after": after, "limit": limit},
    ).fetchall()  # fmt: skip
    return [NodeChange.model_validate(R.node_change_data(r)) for r in rows]


def list_edge_changes(
    conn: Connection,
    comparison_id: int,
    *,
    changes: Sequence[EdgeChangeKind] = (),
    kinds: Sequence[EdgeKind] = (),
    after: int | None = None,
    limit: int,
) -> list[tuple[int, EdgeChange]]:
    """(row id, edge change) in canonical order, optionally of some change kinds and/or
    edge kinds. ``unchanged`` matches no row (unchanged edges are only counted)."""
    rows = conn.execute(
        f"SELECT {R.EDGE_CHANGE_COLUMNS}, r.id{R.EDGE_CHANGE_FROM} WHERE r.comparison_id = %(c)s "
        "AND (cardinality(%(changes)s::text[]) = 0 OR r.change = ANY(%(changes)s)) "
        "AND (cardinality(%(kinds)s::text[]) = 0 OR r.kind = ANY(%(kinds)s)) "
        "AND (%(after)s::bigint IS NULL OR r.id > %(after)s) ORDER BY r.id LIMIT %(limit)s",
        {"c": comparison_id, "changes": [c.value for c in changes],
         "kinds": [k.value for k in kinds], "after": after, "limit": limit},
    ).fetchall()  # fmt: skip
    return [(int(r[7]), EdgeChange.model_validate(R.edge_change_data(r))) for r in rows]
