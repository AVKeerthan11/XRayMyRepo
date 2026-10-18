"""Migration 0005 (change risk) on a real PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database (the
schema is dropped and recreated). Assessments are computed by the pure algorithm from
the comparison fixture and written with a raw-SQL test loader: this checks that the
schema accepts exactly what the contract produces and rejects what it forbids. It is not
the product's persistence path (that is ``xraymyrepo.persistence.change_risk``).
"""

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    ChangeRiskAssessment,
    RepositoryRef,
    RiskEvidence,
    SnapshotDocument,
)
from xraymyrepo.insights.change_risk import change_risk
from xraymyrepo.persistence.queries import REQUIRED_MIGRATIONS

from .conftest import MIGRATIONS, ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import compare_snapshots
    from xraymyrepo.persistence import load_comparison, persist_snapshot

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
_hashes = itertools.count(1)
Rows = list[dict[str, Any]]


def fresh_hash() -> str:
    """A params hash no other run in this module uses (distinct identities)."""
    return "sha256:" + f"{next(_hashes):064x}"


def with_identity(doc: SnapshotDocument, **identity: Any) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    return SnapshotDocument.model_validate(data)


# ---------------------------------------------------------------------------
# Fixtures and the test loader


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        reset_database(connection)
        yield connection


@pytest.fixture(scope="module")
def docs() -> dict[str, SnapshotDocument]:
    before = analyze(FIXTURE / "before", repository=REPO, commit_sha="1" * 40)
    after = analyze(FIXTURE / "after", repository=REPO, commit_sha="2" * 40)
    return {
        "before": before,
        "after": after,
        "other_version": with_identity(after, commit_sha="3" * 40, extractor_version="0.3.0"),
    }


@pytest.fixture(scope="module")
def ids(conn: Any, docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    found = {name: persist_snapshot(conn, doc) for name, doc in docs.items()}
    found["forward"] = compare_snapshots(conn, found["before"], found["after"])
    found["incomparable"] = compare_snapshots(conn, found["before"], found["other_version"])
    found["pending"] = conn.execute(
        "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
        "algorithm_version, params_hash, params) VALUES (%s, %s, 'comparison', '9.9.9', %s, "
        "'{}') RETURNING id",
        (found["after"], found["before"], fresh_hash()),
    ).fetchone()[0]
    return found


@pytest.fixture(scope="module")
def assessments(conn: Any, ids: dict[str, int],
                docs: dict[str, SnapshotDocument]) -> dict[str, ChangeRiskAssessment]:  # fmt: skip
    return {
        "forward": change_risk(load_comparison(conn, ids["forward"]), docs["before"],
                               docs["after"]),
        "incomparable": change_risk(load_comparison(conn, ids["incomparable"]),
                                    docs["before"], docs["other_version"]),
    }  # fmt: skip


def caveats(values: Any) -> str:
    return json.dumps([v.value for v in values])


def insert_run(conn: Any, cid: int, a: ChangeRiskAssessment, *, run_caveats: str | None = None,
               params_hash: str | None = None, **columns: Any) -> int:  # fmt: skip
    """The pending run row."""
    data = {"comparison_id": cid, "algorithm": a.run.algorithm,
            "algorithm_version": a.run.algorithm_version,
            "params_hash": params_hash or fresh_hash(), "params": "{}",
            "medium_at": a.run.medium_at, "high_at": a.run.high_at,
            "caveats": caveats(a.caveats) if run_caveats is None else run_caveats,
            "excluded_by_origin": a.excluded_by_origin, **columns}  # fmt: skip
    names = ", ".join(data)
    marks = ", ".join(f"%({k})s" for k in data)
    row = conn.execute(f"INSERT INTO change_risk_run ({names}) VALUES ({marks}) RETURNING id",
                       data).fetchone()  # fmt: skip
    return int(row[0])


def item_rows(rid: int, cid: int, a: ChangeRiskAssessment) -> Rows:
    return [{"run_id": rid, "comparison_id": cid, "subject_key": i.subject_key,
             "change": i.change.value, "level": i.level.value, "points": i.points,
             "confidence": i.confidence.value, "caveats": caveats(i.caveats)}
            for i in a.items]  # fmt: skip


def factor_rows(rid: int, a: ChangeRiskAssessment) -> Rows:
    def evidence(items: tuple[RiskEvidence, ...]) -> str:
        return json.dumps([e.model_dump(mode="json") for e in items])

    return [{"run_id": rid, "subject_key": i.subject_key, "rule": f.rule.value,
             "points": f.points, "value": f.value, "confidence": f.confidence.value,
             "message": f.message, "evidence": evidence(f.evidence),
             "caveats": caveats(f.caveats)}
            for i in a.items for f in i.factors]  # fmt: skip


def insert_items(conn: Any, rows: Rows) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO change_risk_item (run_id, comparison_id, subject_key, change, level, "
            "points, confidence, caveats) VALUES (%(run_id)s, %(comparison_id)s, "
            "%(subject_key)s, %(change)s, %(level)s, %(points)s, %(confidence)s, %(caveats)s)",
            rows,
        )


def insert_factors(conn: Any, rows: Rows) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO change_risk_factor (run_id, subject_key, rule, points, value, "
            "confidence, message, evidence, caveats) VALUES (%(run_id)s, %(subject_key)s, "
            "%(rule)s, %(points)s, %(value)s, %(confidence)s, %(message)s, %(evidence)s, "
            "%(caveats)s)",
            rows,
        )


def result(a: ChangeRiskAssessment) -> dict[str, Any]:
    return {"level": a.level.value, "confidence": a.confidence.value,
            "summary": a.summary().model_dump(mode="json")}  # fmt: skip


def complete(conn: Any, rid: int, values: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE change_risk_run SET status = 'complete', finished_at = now(), level = %s, "
        "confidence = %s, summary = %s WHERE id = %s",
        (values["level"], values["confidence"], json.dumps(values["summary"]), rid),
    )


def attempt(conn: Any, cid: int, a: ChangeRiskAssessment, *,
            items: Callable[[Rows], Rows] | None = None,
            factors: Callable[[Rows], Rows] | None = None,
            outcome: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
            **run: Any) -> int:  # fmt: skip
    """Write ``a`` with its rows/result transformed, then complete it, in one transaction."""
    with conn.transaction():
        rid = insert_run(conn, cid, a, **run)
        item_list, factor_list = item_rows(rid, cid, a), factor_rows(rid, a)
        insert_items(conn, items(item_list) if items else item_list)
        insert_factors(conn, factors(factor_list) if factors else factor_list)
        values = result(a)
        complete(conn, rid, outcome(values) if outcome else values)
    return rid


def rejected(match: str, error: str = "IntegrityError") -> Any:
    assert psycopg is not None
    return pytest.raises(getattr(psycopg.errors, error), match=match)


def count(conn: Any, table: str, rid: int) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table} WHERE run_id = %s", (rid,))
    return int(row.fetchone()[0])


def where(rows: Rows, **match: Any) -> Rows:
    return [r for r in rows if all(r[k] == v for k, v in match.items())]


# ---------------------------------------------------------------------------
# Applying and storing


def test_migrations_are_registered(conn: Any) -> None:
    files = tuple(p.stem for p in sorted(MIGRATIONS.glob("*.sql")))
    assert files == REQUIRED_MIGRATIONS and files[-1] == "0005_change_risk"
    applied = conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
    assert tuple(r[0] for r in applied) == REQUIRED_MIGRATIONS


def test_a_run_is_stored_complete(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["forward"]
    rid = attempt(conn, ids["forward"], a)
    row = conn.execute(
        "SELECT status, level, confidence, summary, caveats, finished_at IS NOT NULL "
        "FROM change_risk_run WHERE id = %s",
        (rid,),
    ).fetchone()
    assert row == ("complete", "high", "low", a.summary().model_dump(mode="json"), [], True)
    assert count(conn, "change_risk_item", rid) == len(a.items) == 12
    assert count(conn, "change_risk_factor", rid) == sum(len(i.factors) for i in a.items)


def test_an_incomparable_run_says_so(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["incomparable"]
    rid = attempt(conn, ids["incomparable"], a)
    row = conn.execute("SELECT caveats, confidence FROM change_risk_run WHERE id = %s", (rid,))
    assert row.fetchone() == (["incomparable_snapshots"], "low")


def test_a_run_can_fail(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    rid = insert_run(conn, ids["forward"], assessments["forward"])
    conn.execute(
        "UPDATE change_risk_run SET status = 'failed', failure_reason = 'boom', "
        "finished_at = now() WHERE id = %s",
        (rid,),
    )
    with rejected("only pending runs can transition"), conn.transaction():
        complete(conn, rid, result(assessments["forward"]))


# ---------------------------------------------------------------------------
# Which comparisons can be assessed


def test_only_complete_comparisons(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    with rejected("must exist and be complete"), conn.transaction():
        insert_run(conn, ids["pending"], assessments["forward"])
    with rejected("must exist and be complete"), conn.transaction():
        insert_run(conn, 10**9, assessments["forward"])


def test_incomparable_snapshots_is_a_caveat_exactly_when_it_applies(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["forward"]
    with rejected("incomparable_snapshots must be a caveat exactly"), conn.transaction():
        insert_run(conn, ids["forward"], a, run_caveats='["incomparable_snapshots"]')
    with rejected("incomparable_snapshots must be a caveat exactly"), conn.transaction():
        insert_run(conn, ids["incomparable"], a, run_caveats="[]")


def test_identity_is_unique(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    params_hash = fresh_hash()
    insert_run(conn, ids["forward"], assessments["forward"], params_hash=params_hash)
    with rejected("change_risk_run_identity_key", "UniqueViolation"), conn.transaction():
        insert_run(conn, ids["forward"], assessments["forward"], params_hash=params_hash)


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"medium_at": 0}, "change_risk_run_thresholds_check"),
        ({"medium_at": 4, "high_at": 4}, "change_risk_run_thresholds_check"),
        ({"excluded_by_origin": -1}, "change_risk_run_excluded_check"),
        ({"params": "[]"}, "change_risk_run_params_check"),
        ({"params_hash": "sha256:xyz"}, "change_risk_run_params_hash_check"),
        ({"algorithm": "Change Risk"}, "change_risk_run_algorithm_check"),
        ({"algorithm_version": "1.0"}, "change_risk_run_algorithm_version_check"),
        (
            {"status": "running", "finished_at": "2026-01-01T00:00:00Z"},
            "change_risk_run_status_check",
        ),
        ({"run_caveats": '["safe"]'}, "change_risk_run_caveats_check"),
        ({"run_caveats": '["impact_bounded", "impact_bounded"]'}, "change_risk_run_caveats_check"),
        ({"run_caveats": '{"a": 1}'}, "change_risk_run_caveats_check"),
    ],
)
def test_run_checks(
    conn: Any,
    ids: dict[str, int],
    assessments: dict[str, ChangeRiskAssessment],
    columns: dict[str, Any],
    constraint: str,
) -> None:
    with rejected(constraint, "CheckViolation"), conn.transaction():
        insert_run(conn, ids["forward"], assessments["forward"], **columns)


# ---------------------------------------------------------------------------
# Row checks (on a pending run)


@pytest.fixture
def pending(conn: Any, ids: dict[str, int],
            assessments: dict[str, ChangeRiskAssessment]) -> dict[str, Any]:  # fmt: skip
    a = assessments["forward"]
    rid = insert_run(conn, ids["forward"], a)
    items, factors = item_rows(rid, ids["forward"], a), factor_rows(rid, a)
    return {"id": rid, "items": items, "factors": factors,
            "item": where(items, subject_key="shop/legacy.py")[0],
            "factor": where(factors, subject_key="shop/legacy.py",
                            rule="risk.change.kind")[0]}  # fmt: skip


@pytest.mark.parametrize(
    ("update", "constraint"),
    [
        ({"change": "renamed"}, "change_risk_item_change_check"),
        ({"level": "safe"}, "change_risk_item_level_check"),
        ({"level": "unknown"}, "change_risk_item_level_check"),
        # Never what the caveats allow either: rejected by both, the first one reported.
        ({"confidence": "certain"}, "change_risk_item_(caveat_)?confidence_check"),
        ({"points": -1}, "change_risk_item_points_check"),
        ({"caveats": '["safe"]', "confidence": "medium"}, "change_risk_item_caveats_check"),
        # confidence is what the caveats allow: no caveat is high, not lower ...
        ({"confidence": "medium"}, "change_risk_item_caveat_confidence_check"),
        # ... and a caveat is never high
        ({"caveats": '["hidden_dependencies"]'}, "change_risk_item_caveat_confidence_check"),
        ({"subject_key": "bad\x01key"}, "change_risk_item_key_check"),
    ],
)
def test_item_checks(
    conn: Any, pending: dict[str, Any], update: dict[str, Any], constraint: str
) -> None:
    with rejected(constraint, "CheckViolation"), conn.transaction():
        insert_items(conn, [{**pending["item"], **update}])


@pytest.mark.parametrize(
    ("update", "constraint"),
    [
        ({"rule": "risk.vibes"}, "change_risk_factor_rule_check"),
        ({"confidence": "certain"}, "change_risk_factor_confidence_check"),
        ({"points": -1}, "change_risk_factor_points_check"),
        ({"value": -1}, "change_risk_factor_points_check"),
        ({"caveats": '["safe"]'}, "change_risk_factor_caveats_check"),
        ({"message": ""}, "change_risk_factor_message_check"),
        ({"message": " padded"}, "change_risk_factor_message_check"),
        ({"message": "line\nbreak"}, "change_risk_factor_message_check"),
        ({"evidence": "[]"}, "change_risk_factor_evidence_check"),
        ({"evidence": json.dumps([{"side": "to", "node_keys": ["a.py"]}] * 21)},
         "change_risk_factor_evidence_check"),
        ({"evidence": "{}"}, "change_risk_factor_evidence_check"),
        ({"rule": "risk.cap.test_file", "points": 1}, "change_risk_factor_cap_check"),
        ({"rule": "risk.test.no_dependent_test", "points": 0, "caveats": "[]"},
         "change_risk_factor_absence_check"),
    ],
)  # fmt: skip
def test_factor_checks(
    conn: Any, pending: dict[str, Any], update: dict[str, Any], constraint: str
) -> None:
    insert_items(conn, [pending["item"]])
    with rejected(constraint, "CheckViolation"), conn.transaction():
        insert_factors(conn, [{**pending["factor"], **update}])


def test_factors_belong_to_an_item(conn: Any, pending: dict[str, Any]) -> None:
    with rejected("change_risk_factor_item_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_factors(conn, [pending["factor"]])  # its item was never written


def test_subjects_are_rows_of_the_runs_own_comparison(
    conn: Any, ids: dict[str, int], pending: dict[str, Any]
) -> None:
    unknown = {**pending["item"], "subject_key": "shop/nowhere.py"}
    with rejected("change_risk_item_subject_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_items(conn, [unknown])  # deferred: fails at commit
    other = {**pending["item"], "comparison_id": ids["incomparable"]}
    with rejected("change_risk_item_run_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_items(conn, [other])


# ---------------------------------------------------------------------------
# Completing checks the rows and the result


def test_completion_requires_the_change_factor(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    def drop(rows: Rows) -> Rows:
        return [r for r in rows if not (r["subject_key"] == "shop/checkout.py"
                                        and r["rule"] == "risk.change.kind")]  # fmt: skip

    def zero(rows: Rows) -> Rows:  # keep points consistent: checkout's only points were it
        return [{**r, "points": 0, "level": "low"} if r["subject_key"] == "shop/checkout.py"
                else r for r in rows]  # fmt: skip

    with rejected("has no risk.change.kind factor"):
        attempt(conn, ids["forward"], assessments["forward"], items=zero, factors=drop)


@pytest.mark.parametrize(
    ("items", "match"),
    [
        (lambda rows: [{**r, "points": r["points"] + 1} if r["subject_key"] == "shop/consts.py"
                       else r for r in rows], "not the sum of its factors"),
        (lambda rows: [{**r, "change": "modified"} if r["subject_key"] == "shop/consts.py"
                       else r for r in rows], "not a file row of the comparison"),
        (lambda rows: [{**r, "level": "high"} if r["subject_key"] == "shop/consts.py"
                       else r for r in rows], "level does not follow from its points"),
        (lambda rows: [{**r, "level": "low"} if r["subject_key"] == "shop/legacy.py"
                       else r for r in rows], "level does not follow from its points"),
    ],
)  # fmt: skip
def test_completion_checks_items(
    conn: Any,
    ids: dict[str, int],
    assessments: dict[str, ChangeRiskAssessment],
    items: Callable[[Rows], Rows],
    match: str,
) -> None:
    with rejected(match):
        attempt(conn, ids["forward"], assessments["forward"], items=items)


def test_completion_requires_a_file_subject(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    """shop/old (a removed directory) is a comparison row, but not a file."""
    a = assessments["forward"]

    def directory(rows: Rows) -> Rows:
        legacy = where(rows, subject_key="shop/consts.py")[0]
        return [*rows, {**legacy, "subject_key": "shop/old"}]

    def its_factor(rows: Rows) -> Rows:
        kind = where(rows, subject_key="shop/consts.py", rule="risk.change.kind")[0]
        return [*rows, {**kind, "subject_key": "shop/old"}]

    def recount(values: dict[str, Any]) -> dict[str, Any]:
        values["summary"]["levels"]["medium"] += 1
        values["summary"]["confidences"]["high"] += 1
        return values

    with rejected("not a file row of the comparison"):
        attempt(conn, ids["forward"], a, items=directory, factors=its_factor, outcome=recount)


def test_completion_requires_factor_caveats_on_the_item(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    def caveat(rows: Rows) -> Rows:
        return [{**r, "caveats": '["impact_bounded"]'} if r["subject_key"] == "shop/legacy.py"
                and r["rule"] == "risk.impact.dependents" else r for r in rows]  # fmt: skip

    with rejected("not a caveat of its item"):
        attempt(conn, ids["forward"], assessments["forward"], factors=caveat)


def test_completion_caps_test_files_only_by_their_cap_factor(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    """A cap factor makes 'low' the only valid level, whatever the points."""

    def cap(rows: Rows) -> Rows:
        kind = where(rows, subject_key="shop/legacy.py", rule="risk.change.kind")[0]
        return [*rows, {**kind, "rule": "risk.cap.test_file", "points": 0}]

    with rejected("level does not follow from its points"):
        attempt(conn, ids["forward"], assessments["forward"], factors=cap)  # legacy stays high

    def low(rows: Rows) -> Rows:
        return [{**r, "level": "low"} if r["subject_key"] == "shop/legacy.py" else r
                for r in rows]  # fmt: skip

    def recount(values: dict[str, Any]) -> dict[str, Any]:
        levels = values["summary"]["levels"]
        levels.update(high=levels["high"] - 1, low=levels["low"] + 1)
        return values

    attempt(conn, ids["forward"], assessments["forward"], items=low, factors=cap,
            outcome=recount)  # fmt: skip


def test_completion_requires_the_caveat_on_every_item_when_incomparable(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["incomparable"]

    def drop(rows: Rows) -> Rows:
        found = []
        for r in rows:
            if r["subject_key"] == "shop/consts.py":  # incomparable_snapshots only: medium
                assert json.loads(r["caveats"]) == ["incomparable_snapshots"]
                r = {**r, "caveats": "[]", "confidence": "high"}
            found.append(r)
        return found

    def recount(values: dict[str, Any]) -> dict[str, Any]:
        confidences = values["summary"]["confidences"]
        confidences.update(high=confidences["high"] + 1, medium=confidences["medium"] - 1)
        return values

    with rejected("every item carries the caveat"):
        attempt(conn, ids["incomparable"], a, items=drop, outcome=recount)


@pytest.mark.parametrize(
    ("outcome", "match"),
    [
        (lambda v: {**v, "summary": {**v["summary"], "levels": {
            **v["summary"]["levels"], "high": 3}}}, "does not count the items"),
        (lambda v: {**v, "summary": {**v["summary"], "confidences": {
            **v["summary"]["confidences"], "low": 0}}}, "does not count the items"),
        (lambda v: {**v, "level": "medium"}, "not the highest item level"),
        (lambda v: {**v, "confidence": "medium"}, "not the lowest its items"),
    ],
)  # fmt: skip
def test_completion_checks_the_result(
    conn: Any,
    ids: dict[str, int],
    assessments: dict[str, ChangeRiskAssessment],
    outcome: Callable[[dict[str, Any]], dict[str, Any]],
    match: str,
) -> None:
    with rejected(match):
        attempt(conn, ids["forward"], assessments["forward"], outcome=outcome)


@pytest.mark.parametrize(
    "summary",
    [
        lambda s: {"levels": s["levels"]},
        lambda s: {**s, "levels": {**s["levels"], "safe": 0}},
        lambda s: {**s, "levels": {k: v for k, v in s["levels"].items() if k != "medium"}},
        lambda s: {**s, "confidences": {**s["confidences"], "low": 1.5}},
        lambda s: {**s, "confidences": {**s["confidences"], "low": -1}},
    ],
)
def test_the_summary_has_every_count(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment], summary: Any
) -> None:
    with rejected("change_risk_run_summary_check", "CheckViolation"):
        attempt(conn, ids["forward"], assessments["forward"],
                outcome=lambda v: {**v, "summary": summary(v["summary"])})  # fmt: skip


def test_a_complete_run_has_its_result(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    rid = insert_run(conn, ids["forward"], assessments["forward"])
    with rejected("change_risk_run_result_required_check", "CheckViolation"), conn.transaction():
        conn.execute("UPDATE change_risk_run SET status = 'complete', finished_at = now() "
                     "WHERE id = %s", (rid,))  # fmt: skip


def test_an_empty_run_is_low_and_high(
    conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    """No items: the highest level is low and nothing lowers confidence."""
    empty = assessments["forward"].model_copy(update={"items": ()})
    rid = attempt(conn, ids["forward"], empty)
    row = conn.execute("SELECT level, confidence FROM change_risk_run WHERE id = %s", (rid,))
    assert row.fetchone() == ("low", "high")


# ---------------------------------------------------------------------------
# Complete runs are immutable


@pytest.fixture
def stored(conn: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]) -> int:
    return attempt(conn, ids["forward"], assessments["forward"])


@pytest.mark.parametrize(
    "assignment",
    [
        "params = '{\"x\": 1}'",
        "medium_at = 3",
        "high_at = 5",
        "caveats = '[\"hidden_dependencies\"]'",
        "excluded_by_origin = 1",
        "level = 'medium'",
        "status = 'failed', failure_reason = 'x'",
        "created_at = now() - interval '1 day'",
    ],
)
def test_a_complete_run_rejects_updates(conn: Any, stored: int, assignment: str) -> None:
    with rejected("immutable|only pending runs"), conn.transaction():
        conn.execute(f"UPDATE change_risk_run SET {assignment} WHERE id = %s", (stored,))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE change_risk_item SET points = points WHERE run_id = %s",
        "DELETE FROM change_risk_item WHERE run_id = %s",
        "UPDATE change_risk_factor SET message = message WHERE run_id = %s",
        "DELETE FROM change_risk_factor WHERE run_id = %s AND rule = 'risk.impact.dependents'",
        "INSERT INTO change_risk_factor SELECT run_id, subject_key, 'risk.health.hub', 0, 0, "
        "'high', 'm', evidence, caveats FROM change_risk_factor WHERE run_id = %s "
        "AND rule = 'risk.change.kind' LIMIT 1",
    ],
)
def test_rows_of_a_complete_run_are_immutable(conn: Any, stored: int, statement: str) -> None:
    assert psycopg is not None
    with rejected("rows are immutable") as raised, conn.transaction():
        conn.execute(statement, (stored,))
    assert raised.value.diag.sqlstate == "23000"


def test_a_complete_run_can_be_deleted_as_a_whole(conn: Any, stored: int) -> None:
    conn.execute("DELETE FROM change_risk_run WHERE id = %s", (stored,))
    assert count(conn, "change_risk_item", stored) == count(conn, "change_risk_factor", stored) == 0


# ---------------------------------------------------------------------------
# Deleting the comparison or either snapshot cascades


def test_deleting_the_comparison_or_a_snapshot_removes_its_runs(
    conn: Any, docs: dict[str, SnapshotDocument]
) -> None:
    before = with_identity(docs["before"], commit_sha="a" * 40)
    after = with_identity(docs["after"], commit_sha="b" * 40)
    a, b = persist_snapshot(conn, before), persist_snapshot(conn, after)
    forward, backward = compare_snapshots(conn, a, b), compare_snapshots(conn, b, a)
    forward_run = attempt(conn, forward, change_risk(load_comparison(conn, forward), before, after))
    backward_run = attempt(conn, backward,
                           change_risk(load_comparison(conn, backward), after, before))  # fmt: skip
    conn.execute("DELETE FROM snapshot_comparison WHERE id = %s", (forward,))
    assert count(conn, "change_risk_item", forward_run) == 0
    assert conn.execute("SELECT count(*) FROM change_risk_run WHERE id = %s",
                        (forward_run,)).fetchone()[0] == 0  # fmt: skip
    conn.execute("DELETE FROM snapshot WHERE id = %s", (a,))  # backward's to side
    assert count(conn, "change_risk_factor", backward_run) == 0
    assert conn.execute("SELECT count(*) FROM change_risk_run WHERE id = %s",
                        (backward_run,)).fetchone()[0] == 0  # fmt: skip
    conn.execute("DELETE FROM snapshot WHERE id = %s", (b,))
