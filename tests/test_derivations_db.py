"""Derivation persistence (``xraymyrepo.persistence.derivations``) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` is set. Every test starts from a freshly
migrated schema holding the golden snapshot and the analyzer's snapshot of the
golden repository.

The invariant under test: ``load_derivation(persist_derivation(d)) == d.canonical()``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.architecture import ArchitectureParams, derive
from xraymyrepo.cim import Derivation, SnapshotDocument, SnapshotStatus

from .conftest import GOLDEN_REPO, PRODUCER_HASH, SHA, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.persistence import (
        DerivationRunExistsError,
        DerivationRunNotCompleteError,
        DerivationRunNotFoundError,
        PersistenceError,
        SnapshotNotCompleteError,
        SnapshotNotFoundError,
        StoredRun,
        find_derivation_run,
        load_derivation,
        persist_derivation,
        persist_snapshot,
    )
    from xraymyrepo.persistence import derivations as dp

DERIVATION_TABLES = ("derivation_run", "lens", "derived_group", "group_member", "group_edge",
                     "node_metric", "finding", "inferred_layer")  # fmt: skip
SNAPSHOT_TABLES = ("snapshot", "node", "edge", "classification", "unresolved_reference",
                   "extraction_issue", "snapshot_producer")  # fmt: skip
PARAMS = ArchitectureParams()


@pytest.fixture(scope="module")
def connection() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                   commit_sha=golden.snapshot.commit_sha)  # fmt: skip


@pytest.fixture
def stored(connection: Any, golden: SnapshotDocument, analyzed: SnapshotDocument) -> dict[str, int]:
    reset_database(connection)
    return {"golden": persist_snapshot(connection, golden),
            "analyzed": persist_snapshot(connection, analyzed)}  # fmt: skip


@pytest.fixture(scope="module")
def golden_derivation(golden: SnapshotDocument) -> Derivation:
    return derive(golden, PARAMS)


def counts(conn: Any, tables: tuple[str, ...]) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def persist(conn: Any, snapshot_id: int, derivation: Derivation) -> int:
    return persist_derivation(conn, snapshot_id, derivation, PARAMS.as_json())


# ---------------------------------------------------------------------------
# Round trip


def test_golden_derivation_round_trips(connection: Any, stored: dict[str, int],
                                       golden_derivation: Derivation) -> None:  # fmt: skip
    snapshot_rows = counts(connection, SNAPSHOT_TABLES)
    run_id = persist(connection, stored["golden"], golden_derivation)
    loaded = load_derivation(connection, run_id)
    assert loaded == golden_derivation.canonical()
    assert loaded.to_canonical_json() == golden_derivation.to_canonical_json()
    assert loaded.model_dump(mode="json") == golden_derivation.model_dump(mode="json")
    assert counts(connection, SNAPSHOT_TABLES) == snapshot_rows  # facts untouched
    rows = counts(connection, DERIVATION_TABLES)
    assert (rows["derived_group"], rows["group_member"], rows["group_edge"], rows["node_metric"],
            rows["finding"], rows["inferred_layer"]) == (
        len(golden_derivation.groups), len(golden_derivation.members),
        len(golden_derivation.group_edges), len(golden_derivation.metrics),
        len(golden_derivation.findings), len(golden_derivation.layers))  # fmt: skip
    run = connection.execute(
        "SELECT status, finished_at IS NOT NULL, params FROM derivation_run WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert run == ("complete", True, PARAMS.as_json())


def test_analyzed_derivation_round_trips(connection: Any, stored: dict[str, int],
                                         analyzed: SnapshotDocument) -> None:  # fmt: skip
    derivation = derive(analyzed, PARAMS)
    run_id = persist(connection, stored["analyzed"], derivation)
    assert load_derivation(connection, run_id) == derivation.canonical()


def test_reload_is_deterministic(connection: Any, stored: dict[str, int],
                                 golden_derivation: Derivation) -> None:  # fmt: skip
    run_id = persist(connection, stored["golden"], golden_derivation)
    first, second = load_derivation(connection, run_id), load_derivation(connection, run_id)
    assert first == second and first.to_canonical_json() == second.to_canonical_json()


# ---------------------------------------------------------------------------
# Identity and preconditions


def test_same_identity_is_persisted_once(connection: Any, stored: dict[str, int],
                                         golden_derivation: Derivation) -> None:  # fmt: skip
    run_id = persist(connection, stored["golden"], golden_derivation)
    before = counts(connection, DERIVATION_TABLES)
    with pytest.raises(DerivationRunExistsError) as raised:
        persist(connection, stored["golden"], golden_derivation)
    assert (raised.value.run_id, raised.value.status) == (run_id, SnapshotStatus.COMPLETE)
    assert counts(connection, DERIVATION_TABLES) == before
    run = golden_derivation.run
    assert find_derivation_run(connection, stored["golden"], run.algorithm,
                               run.algorithm_version, run.params_hash) == StoredRun(
        run_id, SnapshotStatus.COMPLETE)  # fmt: skip


def test_other_params_are_another_run(connection: Any, stored: dict[str, int],
                                      golden: SnapshotDocument,
                                      golden_derivation: Derivation) -> None:  # fmt: skip
    first = persist(connection, stored["golden"], golden_derivation)
    params = ArchitectureParams(name_signals={"service": "domain"})
    other = derive(golden, params)
    second = persist_derivation(connection, stored["golden"], other, params.as_json())
    assert first != second
    assert load_derivation(connection, second) == other.canonical()
    assert load_derivation(connection, first) == golden_derivation.canonical()


def test_params_must_match_the_hash(connection: Any, stored: dict[str, int],
                                    golden_derivation: Derivation) -> None:  # fmt: skip
    with pytest.raises(PersistenceError, match="params"):
        persist_derivation(connection, stored["golden"], golden_derivation, {"layers": []})
    assert set(counts(connection, DERIVATION_TABLES).values()) == {0}


def test_derivation_must_belong_to_the_snapshot(connection: Any, stored: dict[str, int],
                                                golden_derivation: Derivation) -> None:  # fmt: skip
    with pytest.raises(PersistenceError, match="not computed from snapshot"):
        persist(connection, stored["analyzed"], golden_derivation)
    with pytest.raises(SnapshotNotFoundError):
        persist(connection, 10**9, golden_derivation)
    assert set(counts(connection, DERIVATION_TABLES).values()) == {0}


def test_only_complete_snapshots_are_derived(connection: Any, stored: dict[str, int],
                                             golden_derivation: Derivation) -> None:  # fmt: skip
    assert psycopg is not None
    repository = connection.execute("SELECT id FROM repository").fetchone()[0]
    pending = connection.execute(
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config) VALUES (%s, %s, '9.0.0', %s, '1.0', '{}') RETURNING id",
        (repository, SHA, PRODUCER_HASH),
    ).fetchone()[0]
    with pytest.raises(SnapshotNotCompleteError):
        persist(connection, pending, golden_derivation)
    # The database enforces it too (derivation_run_require_complete_snapshot).
    with pytest.raises(psycopg.errors.IntegrityError, match="not complete"):
        connection.execute(
            "INSERT INTO derivation_run (snapshot_id, algorithm, algorithm_version, params_hash, "
            "params) VALUES (%s, 'architecture', '1.0.0', %s, '{}')",
            (pending, PRODUCER_HASH),
        )


# ---------------------------------------------------------------------------
# Rollback


def test_failure_after_rows_were_written_rolls_back(
    connection: Any, stored: dict[str, int], golden_derivation: Derivation
) -> None:
    """A layer citing a node the snapshot lacks fails after lenses and groups were copied."""
    layer = golden_derivation.layers[0].model_copy(update={"node_key": "backend/missing.py"})
    broken = golden_derivation.model_copy(
        update={"layers": (layer, *golden_derivation.layers[1:])})  # fmt: skip
    with pytest.raises(PersistenceError, match="unknown nodes"):
        persist(connection, stored["golden"], broken)
    assert set(counts(connection, DERIVATION_TABLES).values()) == {0}


def test_database_rejection_rolls_back(connection: Any, stored: dict[str, int],
                                       golden_derivation: Derivation) -> None:  # fmt: skip
    """A NaN metric, smuggled past the model validators, fails node_metric_value_check."""
    assert psycopg is not None
    metric = golden_derivation.metrics[0].model_copy(update={"value": math.nan})
    broken = golden_derivation.model_copy(
        update={"metrics": (metric, *golden_derivation.metrics[1:])})  # fmt: skip
    with pytest.raises(psycopg.errors.CheckViolation, match="node_metric_value_check"):
        persist(connection, stored["golden"], broken)
    assert set(counts(connection, DERIVATION_TABLES).values()) == {0}
    run = golden_derivation.run
    assert find_derivation_run(connection, stored["golden"], run.algorithm,
                               run.algorithm_version, run.params_hash) is None  # fmt: skip


def test_run_is_invisible_until_committed(connection: Any, stored: dict[str, int],
                                          golden_derivation: Derivation,
                                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    assert psycopg is not None and DATABASE_URL is not None
    seen: list[int] = []
    write_rows = dp._write_rows

    def observe(cur: Any, run_id: int, snapshot_id: int, d: Derivation) -> None:
        write_rows(cur, run_id, snapshot_id, d)
        with psycopg.connect(DATABASE_URL, autocommit=True) as other:
            seen.append(other.execute("SELECT count(*) FROM derived_group").fetchone()[0])

    monkeypatch.setattr(dp, "_write_rows", observe)
    persist(connection, stored["golden"], golden_derivation)
    assert seen == [0]


# ---------------------------------------------------------------------------
# Immutability


MUTATIONS = [
    ("derivation_run", "UPDATE derivation_run SET params = '{}' WHERE id = %(r)s"),
    ("derivation_run", "UPDATE derivation_run SET status = 'failed', failure_reason = 'x' "
                       "WHERE id = %(r)s"),
    ("lens", "UPDATE lens SET kind = 'package' WHERE run_id = %(r)s AND name = 'directory'"),
    ("derived_group", "UPDATE derived_group SET label = 'x' WHERE run_id = %(r)s"),
    ("derived_group", "UPDATE derived_group SET confidence = 'high' WHERE run_id = %(r)s"),
    ("group_member", "DELETE FROM group_member WHERE run_id = %(r)s"),
    ("group_edge", "UPDATE group_edge SET count = count + 1 WHERE run_id = %(r)s"),
    ("node_metric", "UPDATE node_metric SET value = 0 WHERE run_id = %(r)s"),
    ("finding", "DELETE FROM finding WHERE run_id = %(r)s"),
    ("inferred_layer", "UPDATE inferred_layer SET confidence = 'high' WHERE run_id = %(r)s"),
    ("inferred_layer", "INSERT INTO inferred_layer SELECT l.run_id, l.snapshot_id, n.id, "
                       "l.layer, l.basis, l.confidence, l.producer, l.rule, l.evidence "
                       "FROM inferred_layer l JOIN node n ON n.snapshot_id = l.snapshot_id "
                       "AND n.key = 'backend/app/plugins.py' WHERE l.run_id = %(r)s LIMIT 1"),
]  # fmt: skip


@pytest.mark.parametrize(("table", "statement"), MUTATIONS)
def test_complete_run_rejects_mutation(connection: Any, stored: dict[str, int],
                                       golden_derivation: Derivation, table: str,
                                       statement: str) -> None:  # fmt: skip
    """Each statement matches rows, so it cannot pass vacuously; the error is the guard's."""
    assert psycopg is not None
    run_id = persist(connection, stored["golden"], golden_derivation)
    before = counts(connection, DERIVATION_TABLES)
    with pytest.raises(psycopg.errors.IntegrityError,
                       match=r"immutable|only pending runs") as raised:  # fmt: skip
        connection.execute(statement, {"r": run_id})
    assert raised.value.diag.sqlstate == "23000"
    assert counts(connection, DERIVATION_TABLES) == before
    assert load_derivation(connection, run_id) == golden_derivation.canonical()


def test_deleting_the_snapshot_removes_its_runs(connection: Any, stored: dict[str, int],
                                                golden_derivation: Derivation) -> None:  # fmt: skip
    persist(connection, stored["golden"], golden_derivation)
    connection.execute("DELETE FROM snapshot WHERE id = %s", (stored["golden"],))
    assert set(counts(connection, DERIVATION_TABLES).values()) == {0}


# ---------------------------------------------------------------------------
# Loading errors


def test_load_errors(connection: Any, stored: dict[str, int]) -> None:
    with pytest.raises(DerivationRunNotFoundError):
        load_derivation(connection, 10**9)
    run_id = connection.execute(
        "INSERT INTO derivation_run (snapshot_id, algorithm, algorithm_version, params_hash, "
        "params) VALUES (%s, 'architecture', '9.9.9', %s, '{}') RETURNING id",
        (stored["golden"], PRODUCER_HASH),
    ).fetchone()[0]
    with pytest.raises(DerivationRunNotCompleteError, match="pending"):
        load_derivation(connection, run_id)
