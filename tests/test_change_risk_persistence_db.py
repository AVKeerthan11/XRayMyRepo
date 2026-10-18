"""Change-risk persistence (``persistence.change_risk``) and read queries
(``persistence.change_risk_queries``) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database (the
schema is dropped and recreated). Assessments are computed by the pure algorithm from
the comparison fixture.

The invariant under test: ``load_change_risk(persist_change_risk(a)) == a.canonical()``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    ChangeRiskAssessment,
    Confidence,
    RepositoryRef,
    RiskCaveat,
    RiskLevel,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.insights.change_risk import ChangeRiskParams, change_risk

from .conftest import ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import compare_snapshots
    from xraymyrepo.persistence import (
        ChangeRiskExistsError,
        ChangeRiskNotCompleteError,
        ChangeRiskNotFoundError,
        PersistenceError,
        StoredRun,
        find_change_risk,
        load_change_risk,
        load_comparison,
        persist_change_risk,
        persist_snapshot,
    )
    from xraymyrepo.persistence import change_risk as crp
    from xraymyrepo.persistence import change_risk_queries as q
    from xraymyrepo.persistence.comparisons import (
        ComparisonNotCompleteError,
        ComparisonNotFoundError,
    )

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
TABLES = ("change_risk_run", "change_risk_item", "change_risk_factor")
PARAMS = ChangeRiskParams()


def with_identity(doc: SnapshotDocument, **identity: Any) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    return SnapshotDocument.model_validate(data)


@pytest.fixture(scope="module")
def connection() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        yield conn


@pytest.fixture(scope="module")
def docs() -> dict[str, SnapshotDocument]:
    before = analyze(FIXTURE / "before", repository=REPO, commit_sha="1" * 40)
    after = analyze(FIXTURE / "after", repository=REPO, commit_sha="2" * 40)
    return {
        "before": before,
        "after": after,
        "after_copy": with_identity(after, commit_sha="5" * 40),
        "other_version": with_identity(after, commit_sha="3" * 40, extractor_version="0.3.0"),
    }


@pytest.fixture(scope="module")
def ids(connection: Any, docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    found = {name: persist_snapshot(connection, doc) for name, doc in docs.items()}
    for name, (a, b) in {"forward": ("before", "after"), "backward": ("after", "before"),
                         "same": ("after", "after_copy"),
                         "incomparable": ("before", "other_version")}.items():  # fmt: skip
        found[name] = compare_snapshots(connection, found[a], found[b])
    found["pending_comparison"] = connection.execute(
        "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
        "algorithm_version, params_hash, params) VALUES (%s, %s, 'comparison', '9.9.9', %s, "
        "'{}') RETURNING id",
        (found["before"], found["after"], "sha256:" + "e" * 64),
    ).fetchone()[0]
    return found


SIDES = {"forward": ("before", "after"), "backward": ("after", "before"),
         "same": ("after", "after_copy"), "incomparable": ("before", "other_version")}  # fmt: skip


@pytest.fixture(scope="module")
def assessments(connection: Any, ids: dict[str, int],
                docs: dict[str, SnapshotDocument]) -> dict[str, ChangeRiskAssessment]:  # fmt: skip
    return {
        name: change_risk(load_comparison(connection, ids[name]), docs[a], docs[b])
        for name, (a, b) in SIDES.items()
    }


@pytest.fixture(autouse=True)
def no_runs(connection: Any, ids: dict[str, int]) -> None:
    """Every test starts without change-risk runs (comparisons are shared by the module)."""
    connection.execute("DELETE FROM change_risk_run")


def persist(conn: Any, ids: dict[str, int], name: str, a: ChangeRiskAssessment,
            params: ChangeRiskParams = PARAMS) -> int:  # fmt: skip
    return persist_change_risk(conn, ids[name], a, params.as_json())


def counts(conn: Any) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def nothing_stored(conn: Any) -> bool:
    return set(counts(conn).values()) == {0}


# ---------------------------------------------------------------------------
# Round trip


@pytest.mark.parametrize("name", ["forward", "backward", "same", "incomparable"])
def test_round_trip(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment], name: str
) -> None:
    a = assessments[name]
    rid = persist(connection, ids, name, a)
    loaded = load_change_risk(connection, rid)
    assert loaded == a.canonical()
    assert loaded.model_dump_json() == a.canonical().model_dump_json()
    assert (loaded.level, loaded.confidence, loaded.conclusive, loaded.summary()) == (
        a.level, a.confidence, a.conclusive, a.summary())  # fmt: skip
    factors = sum(len(i.factors) for i in a.items)
    assert counts(connection) == {"change_risk_run": 1, "change_risk_item": len(a.items),
                                  "change_risk_factor": factors}  # fmt: skip
    row = connection.execute(
        "SELECT status, finished_at IS NOT NULL, params, level, confidence "
        "FROM change_risk_run WHERE id = %s",
        (rid,),
    ).fetchone()
    assert row == ("complete", True, PARAMS.as_json(), a.level.value, a.confidence.value)


def test_what_the_round_trip_keeps(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    """Caveats in order, factor evidence with its sides and spans, absence factors
    without points, the incomparable caveat, and conclusive (derived, not stored)."""
    loaded = load_change_risk(connection, persist(connection, ids, "forward",
                                                  assessments["forward"]))  # fmt: skip
    report = next(i for i in loaded.items if i.subject_key == "shop/report.py")
    assert report.caveats == (RiskCaveat.UNDETERMINED_CHANGES, RiskCaveat.INCOMPLETE_EXTRACTION,
                              RiskCaveat.HIDDEN_DEPENDENCIES)  # fmt: skip
    assert (report.confidence, report.conclusive) == (Confidence.LOW, False)
    legacy = next(i for i in loaded.items if i.subject_key == "shop/legacy.py")
    dependents = next(f for f in legacy.factors if f.rule.value == "risk.impact.dependents")
    assert [(e.side.value, e.node_keys, len(e.edge_refs)) for e in dependents.evidence] == [
        ("from", ("shop/pricing.py",), 1), ("from", ("shop/report.py",), 2)]  # fmt: skip
    assert "conclusive" not in {c[0] for c in connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name LIKE 'change_risk_%%'").fetchall()}  # fmt: skip
    incomparable = load_change_risk(connection, persist(connection, ids, "incomparable",
                                                        assessments["incomparable"]))  # fmt: skip
    assert incomparable.caveats == (RiskCaveat.INCOMPARABLE_SNAPSHOTS,)
    assert all(RiskCaveat.INCOMPARABLE_SNAPSHOTS in i.caveats for i in incomparable.items)


def test_an_empty_assessment_round_trips(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["same"]
    assert a.items == () and (a.level, a.confidence) == (RiskLevel.LOW, Confidence.HIGH)
    assert load_change_risk(connection, persist(connection, ids, "same", a)) == a


def test_reload_is_deterministic(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    rid = persist(connection, ids, "forward", assessments["forward"])
    first, second = load_change_risk(connection, rid), load_change_risk(connection, rid)
    assert first == second and first.model_dump_json() == second.model_dump_json()


def test_out_of_order_items_are_stored_canonically(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["forward"]
    shuffled = a.model_copy(update={"items": a.items[::-1]})
    assert load_change_risk(connection, persist(connection, ids, "forward", shuffled)) == a


# ---------------------------------------------------------------------------
# Identity


def test_same_identity_is_persisted_once(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["forward"]
    rid = persist(connection, ids, "forward", a)
    before = counts(connection)
    with pytest.raises(ChangeRiskExistsError) as raised:
        persist(connection, ids, "forward", a)
    assert (raised.value.run_id, raised.value.status) == (rid, SnapshotStatus.COMPLETE)
    assert counts(connection) == before
    run = a.run
    assert find_change_risk(connection, ids["forward"], run.algorithm, run.algorithm_version,
                            run.params_hash) == StoredRun(rid, SnapshotStatus.COMPLETE)  # fmt: skip


def test_other_params_are_another_run(
    connection: Any,
    ids: dict[str, int],
    docs: dict[str, SnapshotDocument],
    assessments: dict[str, ChangeRiskAssessment],
) -> None:
    first = persist(connection, ids, "forward", assessments["forward"])
    params = ChangeRiskParams(medium_at=1, high_at=6)
    other = change_risk(load_comparison(connection, ids["forward"]), docs["before"],
                        docs["after"], params)  # fmt: skip
    second = persist(connection, ids, "forward", other, params)
    assert first != second
    assert load_change_risk(connection, second) == other
    assert load_change_risk(connection, first) == assessments["forward"]


def test_a_pending_run_occupies_its_identity(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    run = assessments["forward"].run
    pending = connection.execute(
        "INSERT INTO change_risk_run (comparison_id, algorithm, algorithm_version, params_hash, "
        "params, medium_at, high_at) VALUES (%s, %s, %s, %s, '{}', 2, 4) RETURNING id",
        (ids["forward"], run.algorithm, run.algorithm_version, run.params_hash),
    ).fetchone()[0]
    with pytest.raises(ChangeRiskExistsError) as raised:
        persist(connection, ids, "forward", assessments["forward"])
    assert (raised.value.run_id, raised.value.status) == (pending, SnapshotStatus.PENDING)


# ---------------------------------------------------------------------------
# Preconditions


def test_params_must_match_the_hash(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    with pytest.raises(PersistenceError, match="params"):
        persist_change_risk(connection, ids["forward"], assessments["forward"], {"medium_at": 3})
    assert nothing_stored(connection)


def test_only_complete_assessments_are_stored(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["forward"]
    pending = a.model_copy(update={"run": a.run.model_copy(
        update={"status": SnapshotStatus.PENDING})})  # fmt: skip
    with pytest.raises(PersistenceError, match="only complete assessments"):
        persist(connection, ids, "forward", pending)
    assert nothing_stored(connection)


def test_the_assessment_must_be_of_the_comparison(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    for name in ("backward", "same", "incomparable"):
        with pytest.raises(PersistenceError, match="not computed from comparison"):
            persist(connection, ids, name, assessments["forward"])
    with pytest.raises(ComparisonNotFoundError):
        persist_change_risk(connection, 10**9, assessments["forward"], PARAMS.as_json())
    with pytest.raises(ComparisonNotCompleteError, match="pending"):
        persist(connection, ids, "pending_comparison", assessments["forward"])
    assert nothing_stored(connection)


# ---------------------------------------------------------------------------
# Rollback: a failure from Python or the database leaves nothing behind


def test_a_database_rejection_rolls_back(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    """A level that does not follow from the points, smuggled past the model: rejected
    when completing, after every row was written."""
    assert psycopg is not None
    a = assessments["forward"]
    legacy = next(i for i in a.items if i.subject_key == "shop/legacy.py")
    lowered = legacy.model_copy(update={"level": RiskLevel.MEDIUM})
    broken = a.model_copy(update={"items": tuple(lowered if i is legacy else i for i in a.items)})
    with pytest.raises(psycopg.errors.IntegrityError, match="does not follow from its points"):
        persist(connection, ids, "forward", broken)
    assert nothing_stored(connection)
    run = a.run
    assert find_change_risk(connection, ids["forward"], run.algorithm, run.algorithm_version,
                            run.params_hash) is None  # fmt: skip


def test_a_subject_outside_the_comparison_rolls_back(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    assert psycopg is not None
    a = assessments["forward"]
    ghost = a.items[0].model_copy(update={"subject_key": "shop/ghost.py"})
    broken = a.model_copy(update={"items": (ghost, *a.items[1:])})
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="change_risk_item_subject_fkey"):
        persist(connection, ids, "forward", broken)
    assert nothing_stored(connection)


def test_a_run_is_invisible_until_committed(
    connection: Any,
    ids: dict[str, int],
    assessments: dict[str, ChangeRiskAssessment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert psycopg is not None and DATABASE_URL is not None
    seen: list[tuple[int, int]] = []
    write_rows = crp._write_rows

    def observe(cur: Any, run_id: int, comparison_id: int, a: ChangeRiskAssessment) -> None:
        write_rows(cur, run_id, comparison_id, a)
        with psycopg.connect(DATABASE_URL, autocommit=True) as other:
            seen.append(other.execute(
                "SELECT (SELECT count(*) FROM change_risk_run), "
                "(SELECT count(*) FROM change_risk_item)").fetchone())  # fmt: skip

    monkeypatch.setattr(crp, "_write_rows", observe)
    persist(connection, ids, "forward", assessments["forward"])
    assert seen == [(0, 0)]


# ---------------------------------------------------------------------------
# Loading


def test_load_errors(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    with pytest.raises(ChangeRiskNotFoundError):
        load_change_risk(connection, 10**9)
    run = assessments["forward"].run
    pending = connection.execute(
        "INSERT INTO change_risk_run (comparison_id, algorithm, algorithm_version, params_hash, "
        "params, medium_at, high_at) VALUES (%s, %s, %s, %s, '{}', 2, 4) RETURNING id",
        (ids["forward"], run.algorithm, run.algorithm_version, run.params_hash),
    ).fetchone()[0]
    with pytest.raises(ChangeRiskNotCompleteError, match="pending"):
        load_change_risk(connection, pending)
    connection.execute(
        "UPDATE change_risk_run SET status = 'failed', failure_reason = 'boom', "
        "finished_at = now() WHERE id = %s",
        (pending,),
    )
    with pytest.raises(ChangeRiskNotCompleteError, match="failed"):
        load_change_risk(connection, pending)


def test_a_run_altered_outside_persistence_is_detected(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    """With the guards bypassed (a superuser's replication role), a changed stored level
    no longer matches its items: loading refuses it rather than serving it."""
    rid = persist(connection, ids, "forward", assessments["forward"])
    with connection.transaction():
        connection.execute("SET LOCAL session_replication_role = replica")
        connection.execute("UPDATE change_risk_run SET level = 'medium' WHERE id = %s", (rid,))
    with pytest.raises(PersistenceError, match="stored results and items differ"):
        load_change_risk(connection, rid)


def test_immutable_after_persisting(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    assert psycopg is not None
    rid = persist(connection, ids, "forward", assessments["forward"])
    for statement in (
        "UPDATE change_risk_run SET summary = summary WHERE id = %s",
        "UPDATE change_risk_item SET points = points WHERE run_id = %s",
        "DELETE FROM change_risk_factor WHERE run_id = %s",
    ):
        with pytest.raises(psycopg.errors.IntegrityError, match=r"immutable|only pending"):
            connection.execute(statement, (rid,))
    assert load_change_risk(connection, rid) == assessments["forward"]


# ---------------------------------------------------------------------------
# Read queries


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    assert psycopg is not None
    executed: list[str] = []
    original = psycopg.Connection.execute

    def counting(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(query))
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", counting)
    return executed


def test_run_records(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    first = persist(connection, ids, "forward", assessments["forward"])
    params = ChangeRiskParams(wide_dependents=2)
    second = persist_change_risk(connection, ids["forward"], assessments["forward"].model_copy(
        update={"run": assessments["forward"].run.model_copy(
            update={"params_hash": params.params_hash()})}), params.as_json())  # fmt: skip
    record = q.get_change_risk(connection, first)
    assert record is not None
    a = assessments["forward"]
    assert (record.comparison_id, record.algorithm, record.medium_at, record.high_at,
            record.caveats, record.level, record.confidence, record.status) == (
        ids["forward"], a.run.algorithm, 2, 4, (), a.level, a.confidence,
        SnapshotStatus.COMPLETE)  # fmt: skip
    assert q.get_change_risk(connection, 10**9) is None
    listed = q.list_change_risks(connection, ids["forward"], limit=10)
    assert [r.id for r in listed] == [second, first]  # newest first
    assert [r.id for r in q.list_change_risks(connection, ids["forward"], after=second,
                                              limit=10)] == [first]  # fmt: skip
    assert q.list_change_risks(connection, ids["backward"], limit=10) == []
    assert q.change_risk_params(connection, first) == PARAMS.as_json()
    assert q.change_risk_summary(connection, first) == a.summary()


def riskiest_first(a: ChangeRiskAssessment) -> list[str]:
    return [i.subject_key for i in sorted(a.items, key=lambda i: (-i.level.rank, -i.points,
                                                                  i.subject_key))]  # fmt: skip


def test_items_are_listed_riskiest_first_and_paged(
    connection: Any,
    ids: dict[str, int],
    assessments: dict[str, ChangeRiskAssessment],
    statements: list[str],
) -> None:
    a = assessments["forward"]
    rid = persist(connection, ids, "forward", a)
    for limit in (1, 5, 100):
        found, after = [], None
        while True:
            statements.clear()
            page = q.list_items(connection, rid, after=after, limit=limit)
            assert len(statements) == (2 if page else 1), statements  # items, then factors
            found += page
            if len(page) < limit:
                break
            after = q.item_sort_key(page[-1])
        assert [i.subject_key for i in found] == riskiest_first(a)
        assert found == sorted(a.items, key=lambda i: (-i.level.rank, -i.points, i.subject_key))


@pytest.mark.parametrize(
    "filters",
    [
        {"levels": [RiskLevel.HIGH]},
        {"levels": [RiskLevel.MEDIUM, RiskLevel.LOW]},
        {"confidences": [Confidence.LOW]},
        {"caveats": [RiskCaveat.UNDETERMINED_CHANGES]},
        {"caveats": [RiskCaveat.INCOMPLETE_EXTRACTION, RiskCaveat.UNDETERMINED_CHANGES]},
        {"levels": [RiskLevel.HIGH], "confidences": [Confidence.MEDIUM]},  # none
    ],
)
def test_item_filters(
    connection: Any,
    ids: dict[str, int],
    assessments: dict[str, ChangeRiskAssessment],
    filters: dict[str, Any],
) -> None:
    a = assessments["forward"]
    rid = persist(connection, ids, "forward", a)

    def wanted(i: Any) -> bool:
        return (
            ("levels" not in filters or i.level in filters["levels"])
            and ("confidences" not in filters or i.confidence in filters["confidences"])
            and ("caveats" not in filters or bool(set(i.caveats) & set(filters["caveats"])))
        )

    expected = {i.subject_key for i in a.items if wanted(i)}
    found = q.list_items(connection, rid, limit=100, **filters)
    assert [i.subject_key for i in found] == [k for k in riskiest_first(a) if k in expected]


def test_one_item(
    connection: Any, ids: dict[str, int], assessments: dict[str, ChangeRiskAssessment]
) -> None:
    a = assessments["forward"]
    rid = persist(connection, ids, "forward", a)
    for expected in a.items:
        assert q.get_item(connection, rid, expected.subject_key) == expected
    assert q.get_item(connection, rid, "shop/nowhere.py") is None
