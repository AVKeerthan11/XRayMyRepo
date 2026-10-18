"""``orchestration.assess_change_risk`` (load -> assess -> persist) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database (the
schema is dropped and recreated). Comparisons of the comparison fixture are stored
through ``compare_snapshots``; every stored assessment is checked against
``change_risk`` run in memory on the same documents.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    ChangeRiskAssessment,
    NodeChangeKind,
    RepositoryRef,
    RiskCaveat,
    RiskFactorRule,
    RiskLevel,
    Side,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.insights import change_risk as algorithm
from xraymyrepo.insights.change_risk import ChangeRiskParams, change_risk
from xraymyrepo.insights.comparison import compare
from xraymyrepo.insights.health import health

from .conftest import ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo import orchestration
    from xraymyrepo.orchestration import assess_change_risk, compare_snapshots
    from xraymyrepo.persistence import (
        ChangeRiskExistsError,
        StoredRun,
        find_change_risk,
        load_change_risk,
        persist_snapshot,
    )
    from xraymyrepo.persistence.comparisons import (
        ComparisonNotCompleteError,
        ComparisonNotFoundError,
    )

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
TABLES = ("change_risk_run", "change_risk_item", "change_risk_factor")


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


PAIRS = {"forward": ("before", "after"), "backward": ("after", "before"),
         "same": ("after", "after_copy"), "incomparable": ("before", "other_version")}  # fmt: skip


@pytest.fixture(scope="module")
def ids(connection: Any, docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    found = {name: persist_snapshot(connection, doc) for name, doc in docs.items()}
    for name, (a, b) in PAIRS.items():
        found[name] = compare_snapshots(connection, found[a], found[b])
    found["pending"] = connection.execute(
        "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
        "algorithm_version, params_hash, params) VALUES (%s, %s, 'comparison', '9.9.9', %s, "
        "'{}') RETURNING id",
        (found["before"], found["after"], "sha256:" + "e" * 64),
    ).fetchone()[0]
    return found


@pytest.fixture(autouse=True)
def no_runs(connection: Any, ids: dict[str, int]) -> None:
    """Every test starts without change-risk runs (comparisons are shared by the module)."""
    connection.execute("DELETE FROM change_risk_run")


def expected(docs: dict[str, SnapshotDocument], name: str,
             params: ChangeRiskParams | None = None) -> ChangeRiskAssessment:  # fmt: skip
    """What the pure algorithm says for the same documents."""
    before, after = (docs[side] for side in PAIRS[name])
    return change_risk(compare(before, after), before, after, params)


def counts(conn: Any) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def nothing_stored(conn: Any) -> bool:
    return set(counts(conn).values()) == {0}


def find(conn: Any, comparison_id: int, params: ChangeRiskParams | None = None) -> Any:
    params = params or ChangeRiskParams()
    return find_change_risk(conn, comparison_id, algorithm.ALGORITHM,
                            algorithm.ALGORITHM_VERSION, params.params_hash())  # fmt: skip


@pytest.fixture
def no_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything is loaded or assessed."""

    def must_not_run(*args: Any) -> None:
        raise AssertionError("loaded or assessed")

    for name in ("change_risk", "load_comparison", "load_snapshot"):
        monkeypatch.setattr(orchestration, name, must_not_run)


# ---------------------------------------------------------------------------
# Creating


@pytest.mark.parametrize("name", ["forward", "backward", "same", "incomparable"])
def test_stores_exactly_the_assessment(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument], name: str
) -> None:
    rid = assess_change_risk(connection, ids[name])
    loaded = load_change_risk(connection, rid)
    assert loaded == expected(docs, name)
    assert loaded.model_dump_json() == expected(docs, name).model_dump_json()
    assert find(connection, ids[name]) == StoredRun(rid, SnapshotStatus.COMPLETE)
    row = connection.execute(
        "SELECT comparison_id, params FROM change_risk_run WHERE id = %s", (rid,)
    ).fetchone()
    assert row == (ids[name], ChangeRiskParams().as_json())


def test_removed_files_are_assessed_in_from_and_the_rest_in_to(
    connection: Any, ids: dict[str, int]
) -> None:
    a = load_change_risk(connection, assess_change_risk(connection, ids["forward"]))
    for item in a.items:
        kind = next(f for f in item.factors if f.rule is RiskFactorRule.CHANGE_KIND)
        if item.change is NodeChangeKind.REMOVED:
            assert [e.side for e in kind.evidence] == [Side.FROM], item.subject_key
            assert RiskFactorRule.NO_DEPENDENT_TEST not in {f.rule for f in item.factors}
        else:
            assert kind.evidence[0].side is Side.TO, item.subject_key
    legacy = next(i for i in a.items if i.subject_key == "shop/legacy.py")
    dependents = next(f for f in legacy.factors if f.rule is RiskFactorRule.IMPACT_DEPENDENTS)
    # Surviving dependents only, cited in from keys.
    assert {e.node_keys[0] for e in dependents.evidence} == {"shop/pricing.py", "shop/report.py"}
    assert all(e.side is Side.FROM for e in dependents.evidence)


def test_health_runs_on_to_and_on_from_only_when_a_removed_file_needs_it(
    connection: Any, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved: health on ``to``, and on ``from`` only for removed subjects; never when
    nothing changed."""
    sides: list[str] = []
    real = health

    def counting(doc: SnapshotDocument, params: Any = None) -> Any:
        sides.append(doc.snapshot.commit_sha[0])
        return real(doc, params)

    monkeypatch.setattr(algorithm, "health", counting)
    assess_change_risk(connection, ids["forward"])  # removed files: both sides
    assert sorted(sides) == ["1", "2"]
    sides.clear()
    connection.execute("DELETE FROM change_risk_run")
    assess_change_risk(connection, ids["same"])  # nothing changed: no subject, no health
    assert sides == []


def test_is_deterministic(connection: Any, ids: dict[str, int]) -> None:
    """Assessing the same identity again (after deleting it) stores the same result."""
    first = load_change_risk(connection, assess_change_risk(connection, ids["forward"]))
    connection.execute("DELETE FROM change_risk_run")
    second = load_change_risk(connection, assess_change_risk(connection, ids["forward"]))
    assert first == second and first.model_dump_json() == second.model_dump_json()


def test_other_params_are_another_run(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    default = assess_change_risk(connection, ids["forward"])
    params = ChangeRiskParams(medium_at=3, high_at=6)
    other = assess_change_risk(connection, ids["forward"], params)
    assert other != default
    assert load_change_risk(connection, other) == expected(docs, "forward", params)
    assert find(connection, ids["forward"], params) == StoredRun(other, SnapshotStatus.COMPLETE)


def test_a_not_fully_comparable_comparison_is_assessed(
    connection: Any, ids: dict[str, int]
) -> None:
    a = load_change_risk(connection, assess_change_risk(connection, ids["incomparable"]))
    assert a.caveats == (RiskCaveat.INCOMPARABLE_SNAPSHOTS,) and not a.conclusive
    assert all(RiskCaveat.INCOMPARABLE_SNAPSHOTS in i.caveats for i in a.items)
    assert next(i for i in a.items if i.subject_key == "shop/legacy.py").level is RiskLevel.HIGH


# ---------------------------------------------------------------------------
# An existing run is never recomputed or replaced


@pytest.mark.parametrize("status", ["complete", "pending", "failed"])
def test_an_existing_run_is_not_recomputed(
    connection: Any, ids: dict[str, int], status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if status == "complete":
        rid = assess_change_risk(connection, ids["forward"])
    else:
        rid = connection.execute(
            "INSERT INTO change_risk_run (comparison_id, algorithm, algorithm_version, "
            "params_hash, params, medium_at, high_at) VALUES (%s, %s, %s, %s, '{}', 2, 4) "
            "RETURNING id",
            (ids["forward"], algorithm.ALGORITHM, algorithm.ALGORITHM_VERSION,
             ChangeRiskParams().params_hash()),
        ).fetchone()[0]  # fmt: skip
        if status == "failed":
            connection.execute(
                "UPDATE change_risk_run SET status = 'failed', failure_reason = 'boom', "
                "finished_at = now() WHERE id = %s",
                (rid,),
            )

    def must_not_run(*args: Any) -> None:
        raise AssertionError("loaded or assessed")

    for name in ("change_risk", "load_comparison", "load_snapshot"):
        monkeypatch.setattr(orchestration, name, must_not_run)
    before = counts(connection)
    with pytest.raises(ChangeRiskExistsError) as raised:
        assess_change_risk(connection, ids["forward"])
    assert (raised.value.run_id, raised.value.status) == (rid, SnapshotStatus(status))
    assert counts(connection) == before


# ---------------------------------------------------------------------------
# Unusable comparisons: rejected before anything is loaded, assessed or stored


def test_a_missing_comparison(connection: Any, no_compute: None) -> None:
    with pytest.raises(ComparisonNotFoundError, match="does not exist"):
        assess_change_risk(connection, 10**9)
    assert nothing_stored(connection)


def test_an_incomplete_comparison(connection: Any, ids: dict[str, int], no_compute: None) -> None:
    with pytest.raises(ComparisonNotCompleteError, match="pending"):
        assess_change_risk(connection, ids["pending"])
    assert nothing_stored(connection)


# ---------------------------------------------------------------------------
# Transaction boundary


def test_a_failed_store_rolls_back_and_frees_the_identity(
    connection: Any, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert psycopg is not None
    real = algorithm.change_risk

    def lowered(*args: Any) -> ChangeRiskAssessment:
        """An item whose level does not follow from its points, past the model."""
        a = real(*args)
        first = a.items[0]
        wrong = RiskLevel.LOW if first.level is not RiskLevel.LOW else RiskLevel.HIGH
        changed = first.model_copy(update={"level": wrong})
        return a.model_copy(update={"items": (changed, *a.items[1:])})

    monkeypatch.setattr(orchestration, "change_risk", lowered)
    with pytest.raises(psycopg.errors.IntegrityError, match="does not follow from its points"):
        assess_change_risk(connection, ids["forward"])
    assert nothing_stored(connection) and find(connection, ids["forward"]) is None

    monkeypatch.setattr(orchestration, "change_risk", real)
    rid = assess_change_risk(connection, ids["forward"])
    assert find(connection, ids["forward"]) == StoredRun(rid, SnapshotStatus.COMPLETE)


def test_inside_a_caller_transaction_it_commits_with_the_caller(
    connection: Any, ids: dict[str, int]
) -> None:
    class Abort(Exception):
        pass

    with pytest.raises(Abort), connection.transaction():
        assess_change_risk(connection, ids["forward"])
        assert counts(connection)["change_risk_run"] == 1
        raise Abort
    assert nothing_stored(connection)


def test_deleting_a_snapshot_removes_its_assessments(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    a = persist_snapshot(connection, with_identity(docs["before"], commit_sha="a" * 40))
    b = persist_snapshot(connection, with_identity(docs["after"], commit_sha="b" * 40))
    assess_change_risk(connection, compare_snapshots(connection, a, b))
    assert counts(connection)["change_risk_run"] == 1
    connection.execute("DELETE FROM snapshot WHERE id = %s", (b,))
    assert nothing_stored(connection)
    connection.execute("DELETE FROM snapshot WHERE id = %s", (a,))
