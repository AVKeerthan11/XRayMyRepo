"""Read queries over stored change-risk runs, for callers that need part of one.

The change-risk counterpart of ``comparison_queries``: one run's record, its params and
summary, and its items a page at a time, with a bounded number of indexed statements,
returned as CIM records (``RiskItem``, factors included), never by database id. Every
function that takes a ``run_id`` assumes the caller has checked that the run is complete
(``get_change_risk``).

Items are listed riskiest first: by level (high first), then points (most first), then
subject key; keyset-paged after the last item's ``item_sort_key``
(``change_risk_item_rank_idx``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xraymyrepo.cim import (
    ChangeRiskSummary,
    Confidence,
    RiskCaveat,
    RiskItem,
    RiskLevel,
    SnapshotStatus,
)

from . import _change_risk_rows as R
from .snapshots import Connection

ItemSortKey = tuple[int, int, str]
"""(level rank, points, subject key) of an item."""


@dataclass(frozen=True)
class ChangeRiskRecord:
    id: int
    comparison_id: int
    algorithm: str
    algorithm_version: str
    params_hash: str
    medium_at: int
    high_at: int
    caveats: tuple[RiskCaveat, ...]
    excluded_by_origin: int
    level: RiskLevel | None
    """The highest item level; None until complete."""
    confidence: Confidence | None
    """The lowest item confidence, at most what ``caveats`` allow; None until complete."""
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None
    capped_items: bool
    """Some item's level is capped (``risk.cap.test_file``): derived from the factors in
    the same statement, never stored. A run with a capped item is not conclusive."""


_COLUMNS = (
    "id, comparison_id, algorithm, algorithm_version, params_hash, medium_at, high_at, "
    "caveats, excluded_by_origin, level, confidence, status, failure_reason, created_at, "
    "finished_at, EXISTS (SELECT 1 FROM change_risk_factor f WHERE f.run_id = change_risk_run.id "
    "AND f.rule = 'risk.cap.test_file')"
)


def _record(row: Sequence[Any]) -> ChangeRiskRecord:
    return ChangeRiskRecord(
        id=int(row[0]), comparison_id=int(row[1]), algorithm=row[2], algorithm_version=row[3],
        params_hash=row[4], medium_at=row[5], high_at=row[6],
        caveats=tuple(RiskCaveat(c) for c in row[7]), excluded_by_origin=row[8],
        level=None if row[9] is None else RiskLevel(row[9]),
        confidence=None if row[10] is None else Confidence(row[10]),
        status=SnapshotStatus(row[11]), failure_reason=row[12], created_at=row[13],
        finished_at=row[14], capped_items=bool(row[15]),
    )  # fmt: skip


def item_sort_key(item: RiskItem) -> ItemSortKey:
    return (item.level.rank, item.points, item.subject_key)


# ---------------------------------------------------------------------------
# Runs


def get_change_risk(conn: Connection, run_id: int) -> ChangeRiskRecord | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM change_risk_run WHERE id = %s", (run_id,))
    found = row.fetchone()
    return None if found is None else _record(found)


def list_change_risks(
    conn: Connection, comparison_id: int, *, after: int | None = None, limit: int
) -> list[ChangeRiskRecord]:
    """Runs of one comparison (any status), newest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM change_risk_run WHERE comparison_id = %s "
        "AND (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
        (comparison_id, after, after, limit),
    ).fetchall()
    return [_record(r) for r in rows]


def change_risk_params(conn: Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT params FROM change_risk_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None, f"change risk run {run_id} does not exist"
    params: dict[str, Any] = row[0]
    return params


def change_risk_summary(conn: Connection, run_id: int) -> ChangeRiskSummary:
    """Items by level and by confidence (complete runs only)."""
    row = conn.execute("SELECT summary FROM change_risk_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None and row[0] is not None, f"change risk run {run_id} is not complete"
    return ChangeRiskSummary.model_validate(row[0])


# ---------------------------------------------------------------------------
# Items


def list_items(
    conn: Connection,
    run_id: int,
    *,
    levels: Sequence[RiskLevel] = (),
    confidences: Sequence[Confidence] = (),
    caveats: Sequence[RiskCaveat] = (),
    after: ItemSortKey | None = None,
    limit: int,
) -> list[RiskItem]:
    """Items riskiest first, optionally of some levels, confidences and/or with any of
    some caveats; each with its factors. Two statements whatever the page size."""
    rank, points, key = after if after is not None else (None, None, None)
    rows = conn.execute(
        f"SELECT {R.ITEM_COLUMNS}{R.ITEM_FROM} WHERE i.run_id = %(run)s "
        "AND (cardinality(%(levels)s::text[]) = 0 OR i.level = ANY(%(levels)s)) "
        "AND (cardinality(%(confidences)s::text[]) = 0 OR i.confidence = ANY(%(confidences)s)) "
        "AND (cardinality(%(caveats)s::text[]) = 0 OR i.caveats ?| %(caveats)s::text[]) "
        "AND (%(key)s::text IS NULL OR (-change_risk_level_rank(i.level), -i.points, "
        "i.subject_key) > (-%(rank)s::integer, -%(points)s::integer, %(key)s::text COLLATE \"C\")) "
        "ORDER BY change_risk_level_rank(i.level) DESC, i.points DESC, i.subject_key "
        "LIMIT %(limit)s",
        {"run": run_id, "levels": [v.value for v in levels],
         "confidences": [v.value for v in confidences], "caveats": [v.value for v in caveats],
         "rank": rank, "points": points, "key": key, "limit": limit},
    ).fetchall()  # fmt: skip
    return _with_factors(conn, run_id, rows)


def get_item(conn: Connection, run_id: int, subject_key: str) -> RiskItem | None:
    rows = conn.execute(
        f"SELECT {R.ITEM_COLUMNS}{R.ITEM_FROM} WHERE i.run_id = %s AND i.subject_key = %s",
        (run_id, subject_key),
    ).fetchall()
    found = _with_factors(conn, run_id, rows)
    return found[0] if found else None


def _with_factors(conn: Connection, run_id: int, rows: Sequence[Sequence[Any]]) -> list[RiskItem]:
    if not rows:
        return []
    factors = R.factors_by_subject(
        conn.execute(
            f"SELECT {R.FACTOR_COLUMNS}{R.FACTOR_FROM} WHERE f.run_id = %s "
            "AND f.subject_key = ANY(%s)",
            (run_id, [row[0] for row in rows]),
        ).fetchall()
    )
    return [RiskItem.model_validate(R.item_data(row, factors.get(row[0], []))) for row in rows]
