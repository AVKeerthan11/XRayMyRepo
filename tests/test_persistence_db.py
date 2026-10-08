"""PostgreSQL persistence (``xraymyrepo.persistence``) against a real database.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database
and ``psycopg`` is installed (``pip install -e .[db-tests]``). Every test starts
from a freshly migrated schema.

The invariant under test: ``load_snapshot(persist_snapshot(doc)) == doc.canonical()``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Basis,
    Confidence,
    EdgeKind,
    Node,
    NodeKind,
    RepositoryRef,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.cim.nodes import RepositoryAttributes
from xraymyrepo.cim.provenance import Producer, Provenance
from xraymyrepo.cim.snapshot import (
    CIM_SCHEMA_VERSION,
    DEFAULT_EXCLUSION_POLICY,
    AnalysisConfig,
    SnapshotIdentity,
)

from .conftest import GOLDEN_REPO, PRODUCER_HASH, SHA, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.persistence import (
        RepositoryIdentityError,
        SnapshotExistsError,
        SnapshotNotCompleteError,
        SnapshotNotFoundError,
        StoredSnapshot,
        find_snapshot,
        load_snapshot,
        persist_snapshot,
        snapshots,
    )

TABLES = ("repository", "producer", "snapshot", "snapshot_producer", "node", "edge",
          "classification", "unresolved_reference", "extraction_issue")  # fmt: skip


@pytest.fixture(scope="module")
def connection() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def conn(connection: Any) -> Any:
    reset_database(connection)
    return connection


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                   commit_sha=golden.snapshot.commit_sha)  # fmt: skip


def row_counts(conn: Any) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def minimal_document(repository: RepositoryRef | None = None) -> SnapshotDocument:
    """The smallest valid snapshot: one repository node, no coverage, nothing else."""
    config = AnalysisConfig(languages=("python",), exclusion_policy=DEFAULT_EXCLUSION_POLICY)
    return SnapshotDocument(
        schema_version=CIM_SCHEMA_VERSION,
        snapshot=SnapshotIdentity(
            repository=repository or RepositoryRef(host="example.com", owner="Acme", name="Empty"),
            commit_sha=SHA,
            extractor_version="1.0.0",
            config_hash=config.config_hash(),
        ),
        config=config,
        producers=(
            Producer(name="structure-extractor", version="1.0.0", config_hash=PRODUCER_HASH),
        ),
        nodes=(
            Node(
                key="/",
                kind=NodeKind.REPOSITORY,
                name="Empty",
                parent_key=None,
                basis=Basis.OBSERVED,
                confidence=Confidence.HIGH,
                provenance=Provenance(producer="structure-extractor"),
                attributes=RepositoryAttributes(),
            ),
        ),
    )


def with_extractor_version(doc: SnapshotDocument, version: str) -> SnapshotDocument:
    return doc.model_copy(
        update={"snapshot": doc.snapshot.model_copy(update={"extractor_version": version})}
    )


# ---------------------------------------------------------------------------
# Round trip


def assert_round_trip(conn: Any, doc: SnapshotDocument) -> int:
    snapshot_id = persist_snapshot(conn, doc)
    loaded = load_snapshot(conn, snapshot_id)
    assert isinstance(loaded, SnapshotDocument)  # constructed, so fully re-validated
    assert loaded == doc.canonical()
    assert loaded.to_canonical_json() == doc.to_canonical_json()
    # Also exact with defaults included: nothing was dropped as "default".
    assert loaded.model_dump(mode="json") == doc.canonical().model_dump(mode="json")
    return snapshot_id


def test_analyzed_golden_fixture_round_trips(conn: Any, analyzed: SnapshotDocument) -> None:
    snapshot_id = assert_round_trip(conn, analyzed)
    counts = row_counts(conn)
    assert (counts["node"], counts["edge"], counts["classification"],
            counts["unresolved_reference"], counts["extraction_issue"],
            counts["snapshot_producer"]) == (
        len(analyzed.nodes), len(analyzed.edges), len(analyzed.classifications),
        len(analyzed.unresolved_references), len(analyzed.extraction_issues),
        len(analyzed.producers))  # fmt: skip
    status = conn.execute(
        "SELECT status, finished_at IS NOT NULL FROM snapshot WHERE id = %s", (snapshot_id,)
    ).fetchone()
    assert status == ("complete", True)


def test_golden_document_round_trips(conn: Any, golden: SnapshotDocument) -> None:
    """The contract fixture covers what the analyzer does not emit yet: every edge kind,
    ambiguity groups, contested and role classifications, endpoints, derived evidence."""
    assert {e.kind for e in golden.edges} >= {EdgeKind.IMPORTS, EdgeKind.REQUIRES,
                                              EdgeKind.INHERITS, EdgeKind.HANDLES,
                                              EdgeKind.TESTS}  # fmt: skip
    assert_round_trip(conn, golden)


def test_exclusion_policy_order_round_trips(conn: Any, golden_data: dict[str, Any]) -> None:
    """JSONB reorders object keys; the canonical form fixes the policy's order."""
    policy = golden_data["config"]["exclusion_policy"]
    golden_data["config"]["exclusion_policy"] = dict(reversed(policy.items()))
    assert_round_trip(conn, SnapshotDocument.model_validate(golden_data))


def test_snapshots_are_scoped(
    conn: Any, golden: SnapshotDocument, analyzed: SnapshotDocument
) -> None:
    """Two snapshots of the same repository and commit share node keys but not rows."""
    first = persist_snapshot(conn, golden)
    second = persist_snapshot(conn, analyzed)
    assert first != second
    assert load_snapshot(conn, first) == golden.canonical()
    assert load_snapshot(conn, second) == analyzed.canonical()
    assert row_counts(conn)["repository"] == 1


def test_minimal_snapshot_round_trips(conn: Any) -> None:
    doc = minimal_document()
    assert not doc.coverage and not doc.edges and not doc.classifications
    assert_round_trip(conn, doc)


def test_reload_is_deterministic(conn: Any, golden: SnapshotDocument) -> None:
    snapshot_id = persist_snapshot(conn, golden)
    first, second = load_snapshot(conn, snapshot_id), load_snapshot(conn, snapshot_id)
    assert first == second
    assert first.to_canonical_json() == second.to_canonical_json()


# ---------------------------------------------------------------------------
# Lifecycle and rollback


def test_python_failure_mid_persist_rolls_back(
    conn: Any, analyzed: SnapshotDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> None:
        raise RuntimeError("injected failure after nodes were written")

    monkeypatch.setattr(snapshots, "_insert_edges", fail)
    with pytest.raises(RuntimeError, match="injected"):
        persist_snapshot(conn, analyzed)
    assert set(row_counts(conn).values()) == {0}
    assert find_snapshot(conn, analyzed.snapshot) is None

    monkeypatch.undo()
    assert_round_trip(conn, analyzed)  # the identity is free to be persisted again


def test_database_failure_mid_persist_rolls_back(conn: Any, golden: SnapshotDocument) -> None:
    """A row the database rejects (here an 'observed' IMPORTS edge, smuggled past the
    Python validators with model_copy) aborts the whole snapshot."""
    assert psycopg is not None
    edges = list(golden.edges)
    index = next(i for i, e in enumerate(edges) if e.kind is EdgeKind.IMPORTS)
    edges[index] = edges[index].model_copy(
        update={"basis": Basis.OBSERVED, "confidence": Confidence.HIGH}
    )
    broken = golden.model_copy(update={"edges": tuple(edges)})
    with pytest.raises(psycopg.errors.CheckViolation, match="edge_kind_basis_check"):
        persist_snapshot(conn, broken)
    assert set(row_counts(conn).values()) == {0}


def test_snapshot_is_invisible_until_committed(
    conn: Any, analyzed: SnapshotDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert psycopg is not None and DATABASE_URL is not None
    seen: list[tuple[str, int]] = []
    complete = snapshots._complete_snapshot

    def observe(cur: Any, snapshot_id: int) -> None:
        own = cur.execute("SELECT status FROM snapshot WHERE id = %s", (snapshot_id,)).fetchone()
        assert own == ("pending",)  # inside the transaction: pending, rows written
        complete(cur, snapshot_id)
        with psycopg.connect(DATABASE_URL, autocommit=True) as other:
            for table in ("snapshot", "node"):
                seen.append((table, other.execute(f"SELECT count(*) FROM {table}").fetchone()[0]))

    monkeypatch.setattr(snapshots, "_complete_snapshot", observe)
    snapshot_id = persist_snapshot(conn, analyzed)
    assert seen == [("snapshot", 0), ("node", 0)]  # not even after completion, until commit
    assert find_snapshot(conn, analyzed.snapshot) == StoredSnapshot(
        snapshot_id, SnapshotStatus.COMPLETE
    )


def test_caller_transaction_controls_commit(conn: Any) -> None:
    doc = minimal_document()
    with pytest.raises(RuntimeError), conn.transaction():
        persist_snapshot(conn, doc)
        raise RuntimeError("caller aborts")
    assert set(row_counts(conn).values()) == {0}


# ---------------------------------------------------------------------------
# Immutability (snapshot_guard_immutable and snapshot_child_guard_immutable)


MUTATIONS = [
    ("snapshot", "UPDATE snapshot SET status = 'failed', failure_reason = 'x' WHERE id = %(s)s"),
    ("snapshot", "UPDATE snapshot SET coverage = '[]' WHERE id = %(s)s"),
    ("node", "UPDATE node SET attributes = '{}' WHERE snapshot_id = %(s)s AND kind = 'file'"),
    ("node", "UPDATE node SET evidence = '[]' WHERE snapshot_id = %(s)s AND kind = 'type'"),
    ("node", "DELETE FROM node WHERE snapshot_id = %(s)s AND kind = 'function' "
             "AND NOT EXISTS (SELECT 1 FROM node c WHERE c.snapshot_id = node.snapshot_id "
             "AND (c.parent_id = node.id OR c.file_id = node.id)) "
             "AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.snapshot_id = node.snapshot_id "
             "AND node.id IN (e.source_id, e.target_id)) "
             "AND NOT EXISTS (SELECT 1 FROM classification c WHERE c.snapshot_id = "
             "node.snapshot_id AND c.node_id = node.id) "
             "AND NOT EXISTS (SELECT 1 FROM unresolved_reference u WHERE u.snapshot_id = "
             "node.snapshot_id AND node.id IN (u.source_id, u.file_id))"),
    ("edge", "UPDATE edge SET occurrence_count = occurrence_count + 1 WHERE snapshot_id = %(s)s"),
    ("edge", "DELETE FROM edge WHERE snapshot_id = %(s)s AND kind = 'TESTS'"),
    ("classification", "INSERT INTO classification (snapshot_id, node_id, facet, value, basis, "
                       "confidence, evidence) SELECT snapshot_id, node_id, 'layer', 'extra', "
                       "basis, confidence, evidence FROM classification "
                       "WHERE snapshot_id = %(s)s AND facet = 'origin' LIMIT 1"),
    ("classification", "UPDATE classification SET contested = NOT contested "
                       "WHERE snapshot_id = %(s)s"),
    ("unresolved_reference", "UPDATE unresolved_reference SET raw_text = 'x' "
                             "WHERE snapshot_id = %(s)s"),
    ("extraction_issue", "DELETE FROM extraction_issue WHERE snapshot_id = %(s)s"),
    ("snapshot_producer", "UPDATE snapshot_producer SET producer_name = producer_name "
                          "WHERE snapshot_id = %(s)s"),
]  # fmt: skip


@pytest.mark.parametrize(("table", "statement"), MUTATIONS)
def test_completed_snapshot_rejects_mutation(
    conn: Any, golden: SnapshotDocument, table: str, statement: str
) -> None:
    """Every statement matches rows of the snapshot; one that matched nothing would not
    raise, so this cannot pass vacuously. The error is the guard's (23000), not a
    CHECK or foreign key."""
    assert psycopg is not None
    snapshot_id = persist_snapshot(conn, golden)
    before = row_counts(conn)
    with pytest.raises(psycopg.errors.IntegrityError,
                       match=r"immutable|only pending snapshots") as raised:  # fmt: skip
        conn.execute(statement, {"s": snapshot_id})
    assert raised.value.diag.sqlstate == "23000"
    assert table in str(raised.value) or table == "snapshot"
    assert row_counts(conn) == before
    assert load_snapshot(conn, snapshot_id) == golden.canonical()


def test_pending_snapshot_rows_are_writable(conn: Any) -> None:
    """The guard applies to finished snapshots only; a pending one is being loaded."""
    repo = conn.execute(
        "INSERT INTO repository (host, owner, name) VALUES ('h', 'o', 'n') RETURNING id"
    ).fetchone()[0]
    snapshot_id = conn.execute(
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config) VALUES (%s, %s, '1.0.0', %s, '1.0', '{}') RETURNING id",
        (repo, SHA, PRODUCER_HASH),
    ).fetchone()[0]
    producer = conn.execute(
        "INSERT INTO producer (name, version, config_hash) VALUES ('p', '1.0.0', %s) "
        "RETURNING id", (PRODUCER_HASH,)
    ).fetchone()[0]  # fmt: skip
    conn.execute("INSERT INTO snapshot_producer VALUES (%s, %s, 'p')", (snapshot_id, producer))
    conn.execute("DELETE FROM snapshot_producer WHERE snapshot_id = %s", (snapshot_id,))
    with pytest.raises(SnapshotNotCompleteError, match="pending"):
        load_snapshot(conn, snapshot_id)


def test_deleting_a_whole_snapshot_still_cascades(conn: Any) -> None:
    """0001 declares ON DELETE CASCADE; the row guard does not block removing a snapshot
    as a unit (retention), only changing what a kept snapshot says."""
    snapshot_id = persist_snapshot(conn, minimal_document())
    conn.execute("DELETE FROM snapshot WHERE id = %s", (snapshot_id,))
    counts = row_counts(conn)
    assert (counts["snapshot"], counts["node"], counts["snapshot_producer"]) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Identity and repeated persistence


def test_same_identity_is_persisted_once(conn: Any, golden: SnapshotDocument) -> None:
    snapshot_id = persist_snapshot(conn, golden)
    before = row_counts(conn)
    with pytest.raises(SnapshotExistsError) as raised:
        persist_snapshot(conn, golden)
    assert (raised.value.snapshot_id, raised.value.status) == (snapshot_id, "complete")
    assert row_counts(conn) == before
    assert find_snapshot(conn, golden.snapshot) == StoredSnapshot(
        snapshot_id, SnapshotStatus.COMPLETE
    )


def test_new_identity_is_a_new_snapshot(conn: Any, golden: SnapshotDocument) -> None:
    """Re-analysis under another extractor version is a separate snapshot; the
    repository and producer rows are shared."""
    first = persist_snapshot(conn, golden)
    rerun = with_extractor_version(golden, "1.0.1")
    second = persist_snapshot(conn, rerun)
    assert first != second
    counts = row_counts(conn)
    assert (counts["snapshot"], counts["repository"]) == (2, 1)
    assert counts["producer"] == len(golden.producers)
    assert load_snapshot(conn, second) == rerun.canonical()
    assert load_snapshot(conn, first) == golden.canonical()


def test_repository_spelled_differently_is_rejected(conn: Any) -> None:
    persist_snapshot(conn, minimal_document())
    before = row_counts(conn)
    other = with_extractor_version(
        minimal_document(RepositoryRef(host="example.com", owner="acme", name="EMPTY")), "2.0.0"
    )
    with pytest.raises(RepositoryIdentityError, match=r"example\.com/Acme/Empty"):
        persist_snapshot(conn, other)
    assert row_counts(conn) == before


def test_find_snapshot_needs_the_exact_identity(conn: Any, golden: SnapshotDocument) -> None:
    persist_snapshot(conn, golden)
    assert find_snapshot(conn, with_extractor_version(golden, "9.9.9").snapshot) is None


def test_load_unknown_snapshot(conn: Any) -> None:
    with pytest.raises(SnapshotNotFoundError):
        load_snapshot(conn, 12345)
