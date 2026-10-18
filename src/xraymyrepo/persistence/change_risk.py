"""Store change-risk assessments (migration 0005) and load them back.

A ``ChangeRiskAssessment`` is computed elsewhere (``xraymyrepo.insights.change_risk``)
from one complete comparison and its two snapshots; this module only maps it onto the
database and back. Like comparisons:

* ``persist_change_risk`` inserts the run as ``pending``, bulk-loads its items and
  factors and marks it ``complete`` with its level, confidence and summary, in one
  transaction; any failure leaves nothing behind.
* A run's identity is ``(comparison, algorithm, algorithm_version, params_hash)``;
  persisting an identity that exists, in any status, raises ``ChangeRiskExistsError``.
* Complete runs and their rows are immutable (``change_risk_run_guard_immutable``,
  ``change_risk_child_guard_immutable``). Completing checks the items, factors and
  summary against each other and the comparison (``change_risk_check_complete``).
* The run's level, confidence and summary are stored for reading without the items;
  they are derived from the items (and checked on completion and on load).
  ``conclusive`` is never stored.
* Round trip: ``load_change_risk(persist_change_risk(...)) == assessment.canonical()``.

The parameters that produced the run are stored with it (``params``); they must hash to
the run's ``params_hash`` (sha256 of their canonical JSON).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import psycopg
from pydantic import TypeAdapter

from xraymyrepo.cim import (
    ChangeRiskAssessment,
    ChangeRiskSummary,
    ComparisonRun,
    RiskCaveat,
    RiskEvidence,
    SnapshotStatus,
)

from . import _change_risk_rows as R
from . import comparison_queries, queries
from .comparisons import ComparisonNotCompleteError, ComparisonNotFoundError
from .derivations import StoredRun, params_hash
from .snapshots import Connection, PersistenceError

_EVIDENCE: TypeAdapter[tuple[RiskEvidence, ...]] = TypeAdapter(tuple[RiskEvidence, ...])


class ChangeRiskExistsError(PersistenceError):
    """A run with the same identity is stored; a run is never replaced."""

    def __init__(self, run_id: int, status: SnapshotStatus) -> None:
        super().__init__(f"change risk run {run_id} with this identity already exists ({status})")
        self.run_id = run_id
        self.status = status


class ChangeRiskNotFoundError(PersistenceError):
    pass


class ChangeRiskNotCompleteError(PersistenceError):
    pass


def _caveats(caveats: Iterable[RiskCaveat]) -> str:
    return json.dumps([c.value for c in caveats])


def comparison_run(conn: Connection, comparison_id: int) -> ComparisonRun | None:
    """The stored comparison's identity, as the ``ComparisonRun`` an assessment names
    (None when it does not exist)."""
    record = comparison_queries.get_comparison(conn, comparison_id)
    if record is None:
        return None
    before = queries.get_snapshot(conn, record.from_snapshot_id)
    after = queries.get_snapshot(conn, record.to_snapshot_id)
    assert before is not None and after is not None  # comparisons cascade with both
    return ComparisonRun(
        from_snapshot=before.identity,
        to_snapshot=after.identity,
        algorithm=record.algorithm,
        algorithm_version=record.algorithm_version,
        params_hash=record.params_hash,
        incomparable=record.incomparable,
        status=record.status,
    )


# ---------------------------------------------------------------------------
# Writing


def persist_change_risk(
    conn: Connection,
    comparison_id: int,
    assessment: ChangeRiskAssessment,
    params: Mapping[str, Any],
) -> int:
    """Store ``assessment`` of comparison ``comparison_id`` as a complete run; returns its
    id.

    One transaction (a savepoint inside a caller's transaction).
    """
    run = assessment.run
    if run.status is not SnapshotStatus.COMPLETE:
        raise PersistenceError(f"only complete assessments are stored, not {run.status}")
    if params_hash(params) != run.params_hash:
        raise PersistenceError("params do not hash to the assessment's params_hash")
    with conn.transaction(), conn.cursor() as cur:
        stored = comparison_run(conn, comparison_id)
        if stored is None:
            raise ComparisonNotFoundError(f"snapshot comparison {comparison_id} does not exist")
        if stored.status is not SnapshotStatus.COMPLETE:
            raise ComparisonNotCompleteError(
                f"snapshot comparison {comparison_id} is {stored.status}"
            )
        if stored != run.comparison:
            raise PersistenceError(
                f"the assessment was not computed from comparison {comparison_id}"
            )

        row = cur.execute(
            "INSERT INTO change_risk_run (comparison_id, algorithm, algorithm_version, "
            "params_hash, params, medium_at, high_at, caveats, excluded_by_origin) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT change_risk_run_identity_key DO NOTHING RETURNING id",
            (comparison_id, run.algorithm, run.algorithm_version, run.params_hash,
             json.dumps(params, sort_keys=True, ensure_ascii=False), run.medium_at, run.high_at,
             _caveats(assessment.caveats), assessment.excluded_by_origin),
        ).fetchone()  # fmt: skip
        if row is None:
            existing = find_change_risk(conn, comparison_id, run.algorithm,
                                        run.algorithm_version, run.params_hash)  # fmt: skip
            assert existing is not None
            raise ChangeRiskExistsError(existing.id, existing.status)
        run_id = int(row[0])
        canonical = assessment.canonical()
        _write_rows(cur, run_id, comparison_id, canonical)
        cur.execute(
            "UPDATE change_risk_run SET status = 'complete', finished_at = now(), level = %s, "
            "confidence = %s, summary = %s WHERE id = %s AND status = 'pending'",
            (canonical.level.value, canonical.confidence.value,
             canonical.summary().model_dump_json(), run_id),
        )  # fmt: skip
        if cur.rowcount != 1:
            raise PersistenceError(f"change risk run {run_id} is no longer pending")
    return run_id


def _write_rows(
    cur: psycopg.Cursor[Any], run_id: int, comparison_id: int, a: ChangeRiskAssessment
) -> None:
    _copy(
        cur,
        "COPY change_risk_item (run_id, comparison_id, subject_key, change, level, points, "
        "confidence, caveats) FROM STDIN",
        ((run_id, comparison_id, i.subject_key, i.change.value, i.level.value, i.points,
          i.confidence.value, _caveats(i.caveats))
         for i in a.items),
    )  # fmt: skip
    _copy(
        cur,
        "COPY change_risk_factor (run_id, subject_key, rule, points, value, confidence, "
        "message, evidence, caveats) FROM STDIN",
        ((run_id, i.subject_key, f.rule.value, f.points, f.value, f.confidence.value, f.message,
          _EVIDENCE.dump_json(f.evidence).decode(), _caveats(f.caveats))
         for i in a.items for f in i.factors),
    )  # fmt: skip


def _copy(cur: psycopg.Cursor[Any], statement: str, rows: Iterable[tuple[Any, ...]]) -> None:
    with cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


# ---------------------------------------------------------------------------
# Reading


def find_change_risk(
    conn: Connection,
    comparison_id: int,
    algorithm: str,
    algorithm_version: str,
    params_hash: str,
) -> StoredRun | None:
    """The stored run with this identity, in any status."""
    row = conn.execute(
        "SELECT id, status FROM change_risk_run WHERE comparison_id = %s AND algorithm = %s "
        "AND algorithm_version = %s AND params_hash = %s",
        (comparison_id, algorithm, algorithm_version, params_hash),
    ).fetchone()
    return None if row is None else StoredRun(int(row[0]), SnapshotStatus(row[1]))


def load_change_risk(conn: Connection, run_id: int) -> ChangeRiskAssessment:
    """Load a complete run as a validated ``ChangeRiskAssessment``, in canonical order.

    Six queries whatever the size: the run, its comparison and that comparison's two
    snapshots, the items and the factors.
    """
    with conn.transaction(), conn.cursor() as cur:
        header = cur.execute(
            "SELECT comparison_id, algorithm, algorithm_version, params_hash, medium_at, "
            "high_at, caveats, excluded_by_origin, status, level, confidence, summary "
            "FROM change_risk_run WHERE id = %s",
            (run_id,),
        ).fetchone()
        if header is None:
            raise ChangeRiskNotFoundError(f"change risk run {run_id} does not exist")
        (comparison_id, algorithm, version, hash_, medium_at, high_at, caveats, excluded,
         status, level, confidence, summary) = header  # fmt: skip
        if status != SnapshotStatus.COMPLETE:
            raise ChangeRiskNotCompleteError(f"change risk run {run_id} is {status}")
        comparison = comparison_run(conn, comparison_id)
        assert comparison is not None  # the run cascades with its comparison
        items = cur.execute(
            f"SELECT {R.ITEM_COLUMNS}{R.ITEM_FROM} WHERE i.run_id = %s", (run_id,)
        ).fetchall()
        factors = R.factors_by_subject(
            cur.execute(
                f"SELECT {R.FACTOR_COLUMNS}{R.FACTOR_FROM} WHERE f.run_id = %s", (run_id,)
            ).fetchall()
        )

    assessment = ChangeRiskAssessment.model_validate({
        "run": {"comparison": comparison, "algorithm": algorithm, "algorithm_version": version,
                "params_hash": hash_, "medium_at": medium_at, "high_at": high_at,
                "status": SnapshotStatus.COMPLETE},
        "items": [R.item_data(row, factors.get(row[0], [])) for row in items],
        "caveats": caveats,
        "excluded_by_origin": excluded,
    })  # fmt: skip
    # Completing checked these against the items; a difference means the stored run was
    # altered outside persistence.
    if (assessment.level.value, assessment.confidence.value) != (level, confidence) or (
        assessment.summary() != ChangeRiskSummary.model_validate(summary)
    ):
        raise PersistenceError(f"change risk run {run_id}: stored results and items differ")
    return assessment.canonical()
