"""``orchestration.compare_snapshots`` (load -> compare -> persist) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database
(the schema is dropped and recreated). Snapshots are the comparison fixture
(``fixtures/comparison/v1``); every stored comparison is checked against ``compare`` run
in memory on the same documents.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Confidence,
    Incomparability,
    RepositoryRef,
    SnapshotComparison,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.cim.snapshot import CIM_SCHEMA_VERSION
from xraymyrepo.insights import comparison as algorithm
from xraymyrepo.insights.comparison import ComparisonParams, compare

from .conftest import ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo import orchestration
    from xraymyrepo.orchestration import SnapshotsNotComparableError, compare_snapshots
    from xraymyrepo.persistence import (
        ComparisonExistsError,
        SnapshotNotCompleteError,
        SnapshotNotFoundError,
        StoredRun,
        find_comparison,
        load_comparison,
        persist_comparison,
        persist_snapshot,
    )

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
COMPARISON_TABLES = ("snapshot_comparison", "node_change", "edge_change")


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
        "other_version": with_identity(after, commit_sha="3" * 40, extractor_version="0.3.0"),
        "other_repo": with_identity(
            after, repository={"host": "github.com", "owner": "xraymyrepo", "name": "elsewhere"}
        ),
    }


@pytest.fixture(scope="module")
def ids(connection: Any, docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    found = {name: persist_snapshot(connection, doc) for name, doc in docs.items()}
    found["pending"] = connection.execute(
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config) SELECT repository_id, %s, extractor_version, config_hash, "
        "%s, config FROM snapshot WHERE id = %s RETURNING id",
        ("4" * 40, CIM_SCHEMA_VERSION, found["after"]),
    ).fetchone()[0]
    return found


@pytest.fixture(autouse=True)
def no_comparisons(connection: Any, ids: dict[str, int]) -> None:
    """Every test starts without comparisons (snapshots are shared by the module)."""
    connection.execute("DELETE FROM snapshot_comparison")


@pytest.fixture
def no_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the comparison algorithm runs."""

    def must_not_run(*args: Any) -> None:
        raise AssertionError("compare() called")

    monkeypatch.setattr(orchestration, "compare", must_not_run)


def counts(conn: Any) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COMPARISON_TABLES}


def nothing_stored(conn: Any) -> bool:
    return set(counts(conn).values()) == {0}


def find(conn: Any, from_id: int, to_id: int, params: ComparisonParams | None = None) -> Any:
    params = params or ComparisonParams()
    return find_comparison(conn, from_id, to_id, algorithm.ALGORITHM,
                           algorithm.ALGORITHM_VERSION, params.params_hash())  # fmt: skip


# ---------------------------------------------------------------------------
# Creating


def test_stores_exactly_the_comparison(connection: Any, ids: dict[str, int],
                                       docs: dict[str, SnapshotDocument]) -> None:  # fmt: skip
    cid = compare_snapshots(connection, ids["before"], ids["after"])
    expected = compare(docs["before"], docs["after"])
    loaded = load_comparison(connection, cid)
    assert loaded == expected
    assert loaded.model_dump_json() == expected.model_dump_json()
    assert find(connection, ids["before"], ids["after"]) == StoredRun(cid, SnapshotStatus.COMPLETE)
    row = connection.execute(
        "SELECT algorithm, algorithm_version, params_hash, params FROM snapshot_comparison "
        "WHERE id = %s",
        (cid,),
    ).fetchone()
    params = ComparisonParams()
    assert row == (algorithm.ALGORITHM, algorithm.ALGORITHM_VERSION, params.params_hash(),
                   params.as_json())  # fmt: skip


def test_is_deterministic(connection: Any, ids: dict[str, int]) -> None:
    """Computing the same identity again (after deleting it) stores the same comparison."""
    first = load_comparison(connection, compare_snapshots(connection, ids["before"], ids["after"]))
    connection.execute("DELETE FROM snapshot_comparison")
    second = load_comparison(connection, compare_snapshots(connection, ids["before"], ids["after"]))
    assert first == second and first.model_dump_json() == second.model_dump_json()


def test_the_reverse_direction_is_another_comparison(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    forward = compare_snapshots(connection, ids["before"], ids["after"])
    backward = compare_snapshots(connection, ids["after"], ids["before"])
    assert forward != backward
    assert load_comparison(connection, backward) == compare(docs["after"], docs["before"])


def test_other_params_are_another_comparison(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    default = compare_snapshots(connection, ids["before"], ids["after"])
    params = ComparisonParams(min_move_symbols=5)
    other = compare_snapshots(connection, ids["before"], ids["after"], params)
    assert other != default
    assert load_comparison(connection, other) == compare(docs["before"], docs["after"], params)
    assert find(connection, ids["before"], ids["after"], params) == StoredRun(
        other, SnapshotStatus.COMPLETE
    )


def test_not_fully_comparable_snapshots_are_compared(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    cid = compare_snapshots(connection, ids["before"], ids["other_version"])
    loaded = load_comparison(connection, cid)
    assert loaded == compare(docs["before"], docs["other_version"])
    assert loaded.run.incomparable == (Incomparability.EXTRACTOR_VERSION_DIFFERS,)
    assert all(n.confidence is not Confidence.HIGH for n in loaded.nodes)
    assert all(e.confidence is not Confidence.HIGH for e in loaded.edges)


# ---------------------------------------------------------------------------
# An existing comparison is never recomputed or replaced


def test_an_existing_comparison_is_not_recomputed(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument], no_compute: None
) -> None:
    # Stored directly; the orchestration's algorithm is patched to fail.
    stored = algorithm.compare(docs["before"], docs["after"])
    cid = persist_comparison(connection, ids["before"], ids["after"], stored,
                             ComparisonParams().as_json())  # fmt: skip
    before = counts(connection)
    with pytest.raises(ComparisonExistsError) as raised:
        compare_snapshots(connection, ids["before"], ids["after"])
    assert (raised.value.comparison_id, raised.value.status) == (cid, SnapshotStatus.COMPLETE)
    assert counts(connection) == before
    assert load_comparison(connection, cid) == stored


def test_a_pending_or_failed_comparison_occupies_its_identity(
    connection: Any, ids: dict[str, int], no_compute: None
) -> None:
    params = ComparisonParams()
    pending = connection.execute(
        "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
        "algorithm_version, params_hash, params) VALUES (%s, %s, %s, %s, %s, '{}') RETURNING id",
        (ids["before"], ids["after"], algorithm.ALGORITHM, algorithm.ALGORITHM_VERSION,
         params.params_hash()),
    ).fetchone()[0]  # fmt: skip
    for status in (SnapshotStatus.PENDING, SnapshotStatus.FAILED):
        if status is SnapshotStatus.FAILED:
            connection.execute(
                "UPDATE snapshot_comparison SET status = 'failed', failure_reason = 'boom', "
                "finished_at = now() WHERE id = %s",
                (pending,),
            )
        with pytest.raises(ComparisonExistsError) as raised:
            compare_snapshots(connection, ids["before"], ids["after"])
        assert (raised.value.comparison_id, raised.value.status) == (pending, status)
    assert counts(connection)["node_change"] == 0


# ---------------------------------------------------------------------------
# Unusable snapshots: rejected before anything is loaded, computed or stored


@pytest.mark.parametrize(
    ("from_name", "to_name", "error", "match"),
    [
        ("missing", "after", "SnapshotNotFoundError", "does not exist"),
        ("before", "missing", "SnapshotNotFoundError", "does not exist"),
        ("pending", "after", "SnapshotNotCompleteError", "is pending"),
        ("before", "pending", "SnapshotNotCompleteError", "is pending"),
        ("before", "before", "SnapshotsNotComparableError", "with itself"),
        ("before", "other_repo", "SnapshotsNotComparableError", "different repositories"),
        ("other_repo", "after", "SnapshotsNotComparableError", "different repositories"),
    ],
)
def test_unusable_snapshots_are_rejected(
    connection: Any, ids: dict[str, int], no_compute: None, monkeypatch: pytest.MonkeyPatch,
    from_name: str, to_name: str, error: str, match: str,
) -> None:  # fmt: skip
    errors = {
        "SnapshotNotFoundError": SnapshotNotFoundError,
        "SnapshotNotCompleteError": SnapshotNotCompleteError,
        "SnapshotsNotComparableError": SnapshotsNotComparableError,
    }

    def must_not_load(*args: Any) -> None:
        raise AssertionError("load_snapshot() called")

    monkeypatch.setattr(orchestration, "load_snapshot", must_not_load)
    known = {**ids, "missing": 10**9}
    with pytest.raises(errors[error], match=match):
        compare_snapshots(connection, known[from_name], known[to_name])
    assert nothing_stored(connection)


# ---------------------------------------------------------------------------
# Transaction boundary: a failed store leaves nothing behind and frees the identity


def test_a_failed_store_rolls_back_and_frees_the_identity(
    connection: Any, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert psycopg is not None
    real_compare = algorithm.compare

    def miscounted(*args: Any) -> SnapshotComparison:
        c = real_compare(*args)
        return c.model_copy(update={"unchanged_edges": c.unchanged_edges + 1})

    monkeypatch.setattr(orchestration, "compare", miscounted)
    with pytest.raises(psycopg.errors.IntegrityError, match="do not account for every edge"):
        compare_snapshots(connection, ids["before"], ids["after"])
    assert nothing_stored(connection)
    assert find(connection, ids["before"], ids["after"]) is None

    monkeypatch.setattr(orchestration, "compare", real_compare)
    cid = compare_snapshots(connection, ids["before"], ids["after"])
    assert find(connection, ids["before"], ids["after"]) == StoredRun(cid, SnapshotStatus.COMPLETE)


def test_inside_a_caller_transaction_it_commits_with_the_caller(
    connection: Any, ids: dict[str, int]
) -> None:
    class Abort(Exception):
        pass

    with pytest.raises(Abort), connection.transaction():
        compare_snapshots(connection, ids["before"], ids["after"])
        assert counts(connection)["snapshot_comparison"] == 1
        raise Abort
    assert nothing_stored(connection)
