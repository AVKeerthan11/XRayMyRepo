"""Comparison persistence (``xraymyrepo.persistence.comparisons``) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database
(the schema is dropped and recreated). Comparisons are computed by the pure algorithm
from the comparison fixture (``fixtures/comparison/v1``).

The invariant under test: ``load_comparison(persist_comparison(c)) == c.canonical()``.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.analyzer import DEFAULT_IGNORE_GLOBS, AnalysisOptions, analyze
from xraymyrepo.cim import (
    Confidence,
    EdgeChangeKind,
    Incomparability,
    NodeChangeKind,
    RepositoryRef,
    SnapshotComparison,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.cim.snapshot import CIM_SCHEMA_VERSION
from xraymyrepo.insights.comparison import ComparisonParams, compare

from .conftest import ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.persistence import (
        ComparisonExistsError,
        ComparisonNotCompleteError,
        ComparisonNotFoundError,
        PersistenceError,
        SnapshotNotCompleteError,
        SnapshotNotFoundError,
        StoredRun,
        find_comparison,
        load_comparison,
        load_comparison_summary,
        persist_comparison,
        persist_snapshot,
    )
    from xraymyrepo.persistence import comparisons as cp

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
COMPARISON_TABLES = ("snapshot_comparison", "node_change", "edge_change")
PARAMS = ComparisonParams()
_commits = itertools.count(16)


def with_identity(doc: SnapshotDocument, **identity: Any) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    return SnapshotDocument.model_validate(data)


def new_commit() -> str:
    return f"{next(_commits):040x}"


# ---------------------------------------------------------------------------
# Fixtures


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
        "after_copy": with_identity(after, commit_sha="5" * 40),  # same content as after
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


@pytest.fixture(scope="module")
def fixture_comparison(docs: dict[str, SnapshotDocument]) -> SnapshotComparison:
    return compare(docs["before"], docs["after"])


def persist(conn: Any, from_id: int, to_id: int, c: SnapshotComparison,
            params: ComparisonParams = PARAMS) -> int:  # fmt: skip
    return persist_comparison(conn, from_id, to_id, c, params.as_json())


def counts(conn: Any) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COMPARISON_TABLES}


def nothing_stored(conn: Any) -> bool:
    return set(counts(conn).values()) == {0}


def assert_round_trips(conn: Any, comparison_id: int, c: SnapshotComparison) -> None:
    loaded = load_comparison(conn, comparison_id)
    assert loaded == c.canonical()
    assert loaded.model_dump(mode="json") == c.canonical().model_dump(mode="json")
    assert loaded.summary() == c.summary() == load_comparison_summary(conn, comparison_id)
    assert loaded.lineage() == c.lineage()


# ---------------------------------------------------------------------------
# Round trip


def test_fixture_comparison_round_trips(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    c = fixture_comparison
    cid = persist(connection, ids["before"], ids["after"], c)
    assert_round_trips(connection, cid, c)
    # Every node is a row, unchanged included; unchanged edges are only counted.
    assert counts(connection) == {"snapshot_comparison": 1, "node_change": len(c.nodes),
                                  "edge_change": len(c.edges)}  # fmt: skip
    row = connection.execute(
        "SELECT status, finished_at IS NOT NULL, params, incomparable, summary "
        "FROM snapshot_comparison WHERE id = %s",
        (cid,),
    ).fetchone()
    assert row == ("complete", True, PARAMS.as_json(), [], c.summary().model_dump(mode="json"))


def test_every_change_kind_survives(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    """The fixture exercises each kind the rows can hold; the loaded records keep them."""
    loaded = load_comparison(connection, persist(connection, ids["before"], ids["after"],
                                                 fixture_comparison))  # fmt: skip
    assert {n.change for n in loaded.nodes} == set(NodeChangeKind)
    assert {e.change for e in loaded.edges} == set(EdgeChangeKind) - {EdgeChangeKind.UNCHANGED}
    undetermined = [n for n in loaded.nodes if n.change is NodeChangeKind.UNDETERMINED]
    assert [(n.from_key, n.to_key, n.match_method, n.confidence) for n in undetermined] == [
        ("shop/report.py#summarize", None, None, Confidence.LOW)]  # fmt: skip
    edge = next(e for e in loaded.edges if e.change is EdgeChangeKind.UNDETERMINED)
    assert (edge.to_ref, edge.confidence) == (None, Confidence.LOW)
    assert edge.from_ref is not None and edge.from_ref.source_key == "shop/report.py"
    ambiguous = sorted(n.key for n in loaded.nodes if n.move_ambiguous)
    assert ambiguous == ["shop/a/template.py", "shop/b/template.py", "shop/c/template.py"]
    moved = {n.from_key: n.to_key for n in loaded.nodes if n.change is NodeChangeKind.MOVED}
    assert moved["shop/old/shipping.py"] == "shop/new/shipping.py"
    assert loaded.unchanged_edges == fixture_comparison.unchanged_edges == 1


def test_reverse_direction_round_trips(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    c = compare(docs["after"], docs["before"])
    assert_round_trips(connection, persist(connection, ids["after"], ids["before"], c), c)


def test_incomparable_comparison_round_trips(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    c = compare(docs["before"], docs["other_version"])
    assert c.run.incomparable == (Incomparability.EXTRACTOR_VERSION_DIFFERS,)
    cid = persist(connection, ids["before"], ids["other_version"], c)
    assert_round_trips(connection, cid, c)
    loaded = load_comparison(connection, cid)
    assert loaded.run.incomparable == c.run.incomparable and not loaded.run.comparable
    assert all(n.confidence is not Confidence.HIGH for n in loaded.nodes)
    # Undetermined with both keys (fingerprints not comparable across versions).
    both = [n for n in loaded.nodes if n.change is NodeChangeKind.UNDETERMINED
            and n.from_key is not None and n.to_key is not None]  # fmt: skip
    assert both and all(n.from_key == n.to_key for n in both)


def test_both_incomparability_reasons_round_trip(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    """Another extractor version and another configuration: both reasons, in enum order,
    are stored (the CHECK's fourth value) and reloaded."""
    narrower = AnalysisOptions(ignore_globs=(*DEFAULT_IGNORE_GLOBS, "shop/a/**"))
    other = with_identity(
        analyze(FIXTURE / "after", repository=REPO, commit_sha=new_commit(), options=narrower),
        extractor_version="0.3.0",
    )
    c = compare(docs["before"], other)
    reasons = (Incomparability.EXTRACTOR_VERSION_DIFFERS, Incomparability.CONFIG_DIFFERS)
    assert c.run.incomparable == reasons
    cid = persist(connection, ids["before"], persist_snapshot(connection, other), c)
    assert_round_trips(connection, cid, c)
    stored = connection.execute(
        "SELECT incomparable FROM snapshot_comparison WHERE id = %s", (cid,)
    ).fetchone()[0]
    assert stored == ["extractor_version_differs", "config_differs"]
    assert all(n.confidence is not Confidence.HIGH for n in load_comparison(connection, cid).nodes)


def test_no_changes_round_trips(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    """Two snapshots of the same content: every node unchanged, no edge rows."""
    c = compare(docs["after"], docs["after_copy"])
    assert not c.edges and c.unchanged_edges == len(docs["after"].edges) > 0
    cid = persist(connection, ids["after"], ids["after_copy"], c)
    assert_round_trips(connection, cid, c)
    assert counts(connection)["edge_change"] == 0
    assert load_comparison_summary(connection, cid).edges[EdgeChangeKind.UNCHANGED] == len(
        docs["after"].edges
    )


def test_snapshots_without_edges_round_trip(connection: Any, tmp_path: Path) -> None:
    """No edges on either side: no edge rows and zero unchanged edges."""
    for state, text in (("one", "# One\n"), ("two", "# Two\n")):
        (tmp_path / state).mkdir()
        (tmp_path / state / "README.md").write_text(text, encoding="utf-8")
    repo = RepositoryRef(host="github.com", owner="xraymyrepo", name="no-edges")
    a = analyze(tmp_path / "one", repository=repo, commit_sha=new_commit())
    b = analyze(tmp_path / "two", repository=repo, commit_sha=new_commit())
    assert not a.edges and not b.edges
    c = compare(a, b)
    cid = persist(connection, persist_snapshot(connection, a), persist_snapshot(connection, b), c)
    assert_round_trips(connection, cid, c)
    loaded = load_comparison(connection, cid)
    assert loaded.edges == () and loaded.unchanged_edges == 0
    assert {n.change for n in loaded.nodes} == {NodeChangeKind.UNCHANGED, NodeChangeKind.MODIFIED}


def test_reload_is_deterministic(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    cid = persist(connection, ids["before"], ids["after"], fixture_comparison)
    first, second = load_comparison(connection, cid), load_comparison(connection, cid)
    assert first == second and first.model_dump_json() == second.model_dump_json()


def test_an_out_of_order_comparison_is_stored_canonically(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    c = fixture_comparison
    shuffled = c.model_copy(update={"nodes": c.nodes[::-1], "edges": c.edges[::-1]})
    cid = persist(connection, ids["before"], ids["after"], shuffled)
    assert load_comparison(connection, cid) == c.canonical()


# ---------------------------------------------------------------------------
# Identity


def test_same_identity_is_persisted_once(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    c = fixture_comparison
    cid = persist(connection, ids["before"], ids["after"], c)
    before = counts(connection)
    with pytest.raises(ComparisonExistsError) as raised:
        persist(connection, ids["before"], ids["after"], c)
    assert (raised.value.comparison_id, raised.value.status) == (cid, SnapshotStatus.COMPLETE)
    assert counts(connection) == before
    run = c.run
    assert find_comparison(connection, ids["before"], ids["after"], run.algorithm,
                           run.algorithm_version, run.params_hash) == StoredRun(
        cid, SnapshotStatus.COMPLETE)  # fmt: skip
    # The identity is directed: the reverse pair is not this comparison.
    assert find_comparison(connection, ids["after"], ids["before"], run.algorithm,
                           run.algorithm_version, run.params_hash) is None  # fmt: skip


def test_other_params_are_another_comparison(
    connection: Any,
    ids: dict[str, int],
    docs: dict[str, SnapshotDocument],
    fixture_comparison: SnapshotComparison,
) -> None:
    first = persist(connection, ids["before"], ids["after"], fixture_comparison)
    params = ComparisonParams(min_move_symbols=5)  # nothing is eligible to move
    other = compare(docs["before"], docs["after"], params)
    assert other.summary().nodes[NodeChangeKind.MOVED] == 0
    second = persist(connection, ids["before"], ids["after"], other, params)
    assert first != second
    assert load_comparison(connection, second) == other.canonical()
    assert load_comparison(connection, first) == fixture_comparison.canonical()


def test_params_must_match_the_hash(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    with pytest.raises(PersistenceError, match="params"):
        persist_comparison(connection, ids["before"], ids["after"], fixture_comparison,
                           {"move_languages": []})  # fmt: skip
    assert nothing_stored(connection)


def test_only_complete_comparisons_are_stored(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    run = fixture_comparison.run.model_copy(update={"status": SnapshotStatus.PENDING})
    pending = fixture_comparison.model_copy(update={"run": run})
    with pytest.raises(PersistenceError, match="only complete comparisons"):
        persist(connection, ids["before"], ids["after"], pending)
    assert nothing_stored(connection)


@pytest.mark.parametrize(
    ("from_name", "to_name", "match"),
    [
        ("after", "before", "is not the comparison's from"),  # swapped
        ("before", "after_copy", "is not the comparison's to"),  # same content, other commit
        ("before", "other_version", "is not the comparison's to"),
        ("before", "before", "is not the comparison's to"),
        ("other_repo", "after", "is not the comparison's from"),
    ],
)
def test_the_comparison_must_be_of_the_snapshots(
    connection: Any,
    ids: dict[str, int],
    fixture_comparison: SnapshotComparison,
    from_name: str,
    to_name: str,
    match: str,
) -> None:
    with pytest.raises(PersistenceError, match=match):
        persist(connection, ids[from_name], ids[to_name], fixture_comparison)
    assert nothing_stored(connection)


def test_both_snapshots_must_exist_and_be_complete(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    for pair in ((10**9, ids["after"]), (ids["before"], 10**9)):
        with pytest.raises(SnapshotNotFoundError):
            persist(connection, *pair, fixture_comparison)
    for pair in ((ids["pending"], ids["after"]), (ids["before"], ids["pending"])):
        with pytest.raises(SnapshotNotCompleteError):
            persist(connection, *pair, fixture_comparison)
    assert nothing_stored(connection)


# ---------------------------------------------------------------------------
# Rollback: a failure from Python or the database leaves nothing behind


def test_a_comparison_citing_unknown_nodes_rolls_back(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    c = fixture_comparison
    added = next(n for n in c.nodes if n.change is NodeChangeKind.ADDED and n.kind.value == "file")
    ghost = added.model_copy(update={"to_key": "shop/ghost.py"})
    broken = c.model_copy(update={"nodes": tuple(ghost if n is added else n for n in c.nodes)})
    with pytest.raises(PersistenceError, match=re.escape("lacks, e.g. 'shop/ghost.py'")):
        persist(connection, ids["before"], ids["after"], broken)
    assert nothing_stored(connection)


def test_a_comparison_citing_unknown_edges_rolls_back(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    c = fixture_comparison
    added = next(e for e in c.edges if e.change is EdgeChangeKind.ADDED)
    assert added.to_ref is not None
    ref = added.to_ref.model_copy(update={"target_key": "shop/util.py"})  # no such edge
    retargeted = added.model_copy(update={"to_ref": ref})
    broken = c.model_copy(update={"edges": tuple(retargeted if e is added else e for e in c.edges)})
    with pytest.raises(PersistenceError, match=r"cites edges snapshot \d+ lacks"):
        persist(connection, ids["before"], ids["after"], broken)
    assert nothing_stored(connection)


def test_a_row_check_violation_rolls_back(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    """move_ambiguous on a function, smuggled past the model validators."""
    assert psycopg is not None
    c = fixture_comparison
    function = next(n for n in c.nodes if n.kind.value == "function")
    flagged = function.model_copy(update={"move_ambiguous": True})
    broken = c.model_copy(update={"nodes": tuple(flagged if n is function else n for n in c.nodes)})
    with pytest.raises(psycopg.errors.CheckViolation, match="node_change_move_ambiguous_check"):
        persist(connection, ids["before"], ids["after"], broken)
    assert nothing_stored(connection)


def test_incomplete_coverage_is_rejected_on_completion(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    """A comparison missing a node row passes the per-row checks; completing rejects it."""
    assert psycopg is not None
    c = fixture_comparison
    dropped = c.model_copy(update={"nodes": tuple(n for n in c.nodes
                                                  if n.key != "shop/consts.py")})  # fmt: skip
    with pytest.raises(psycopg.errors.IntegrityError, match="do not cover both snapshots"):
        persist(connection, ids["before"], ids["after"], dropped)
    assert nothing_stored(connection)


def test_a_wrong_unchanged_edge_count_is_rejected_on_completion(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    assert psycopg is not None
    c = fixture_comparison
    miscounted = c.model_copy(update={"unchanged_edges": c.unchanged_edges + 1})
    with pytest.raises(psycopg.errors.IntegrityError, match="do not account for every edge"):
        persist(connection, ids["before"], ids["after"], miscounted)
    assert nothing_stored(connection)


def test_a_high_confidence_change_between_incomparable_snapshots_is_rejected(
    connection: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    assert psycopg is not None
    c = compare(docs["before"], docs["other_version"])
    first = next(n for n in c.nodes if n.change is NodeChangeKind.UNCHANGED)
    raised = first.model_copy(update={"confidence": Confidence.HIGH})
    broken = c.model_copy(update={"nodes": tuple(raised if n is first else n for n in c.nodes)})
    with pytest.raises(psycopg.errors.IntegrityError, match="not fully comparable"):
        persist(connection, ids["before"], ids["other_version"], broken)
    assert nothing_stored(connection)


def test_comparison_is_invisible_until_committed(
    connection: Any,
    ids: dict[str, int],
    fixture_comparison: SnapshotComparison,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert psycopg is not None and DATABASE_URL is not None
    seen: list[tuple[int, int]] = []
    write_rows = cp._write_rows

    def observe(cur: Any, cid: int, from_id: int, to_id: int, c: SnapshotComparison) -> None:
        write_rows(cur, cid, from_id, to_id, c)
        with psycopg.connect(DATABASE_URL, autocommit=True) as other:
            seen.append(other.execute(
                "SELECT (SELECT count(*) FROM snapshot_comparison), "
                "(SELECT count(*) FROM node_change)").fetchone())  # fmt: skip

    monkeypatch.setattr(cp, "_write_rows", observe)
    persist(connection, ids["before"], ids["after"], fixture_comparison)
    assert seen == [(0, 0)]


# ---------------------------------------------------------------------------
# Immutability and loading errors


def test_a_persisted_comparison_is_immutable(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    assert psycopg is not None
    cid = persist(connection, ids["before"], ids["after"], fixture_comparison)
    for statement in (
        "UPDATE snapshot_comparison SET summary = summary WHERE id = %s",
        "UPDATE node_change SET confidence = confidence WHERE comparison_id = %s",
        "DELETE FROM edge_change WHERE comparison_id = %s",
    ):
        with pytest.raises(psycopg.errors.IntegrityError,
                           match=r"immutable|only pending") as raised:  # fmt: skip
            connection.execute(statement, (cid,))
        assert raised.value.diag.sqlstate == "23000"
    assert_round_trips(connection, cid, fixture_comparison)


def test_load_errors(
    connection: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    for load in (load_comparison, load_comparison_summary):
        with pytest.raises(ComparisonNotFoundError):
            load(connection, 10**9)
    run = fixture_comparison.run
    pending = connection.execute(
        "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
        "algorithm_version, params_hash, params) VALUES (%s, %s, %s, %s, %s, '{}') RETURNING id",
        (ids["before"], ids["after"], run.algorithm, run.algorithm_version, run.params_hash),
    ).fetchone()[0]
    assert find_comparison(connection, ids["before"], ids["after"], run.algorithm,
                           run.algorithm_version, run.params_hash) == StoredRun(
        pending, SnapshotStatus.PENDING)  # fmt: skip
    for load in (load_comparison, load_comparison_summary):
        with pytest.raises(ComparisonNotCompleteError, match="pending"):
            load(connection, pending)
    # A pending (or failed) comparison still occupies its identity.
    with pytest.raises(ComparisonExistsError) as raised:
        persist(connection, ids["before"], ids["after"], fixture_comparison)
    assert raised.value.status is SnapshotStatus.PENDING
    connection.execute(
        "UPDATE snapshot_comparison SET status = 'failed', failure_reason = 'boom', "
        "finished_at = now() WHERE id = %s",
        (pending,),
    )
    with pytest.raises(ComparisonNotCompleteError, match="failed"):
        load_comparison(connection, pending)


def test_deleting_a_snapshot_removes_its_comparisons(
    connection: Any, docs: dict[str, SnapshotDocument]
) -> None:
    a = with_identity(docs["before"], commit_sha=new_commit())
    b = with_identity(docs["after"], commit_sha=new_commit())
    a_id, b_id = persist_snapshot(connection, a), persist_snapshot(connection, b)
    persist(connection, a_id, b_id, compare(a, b))
    connection.execute("DELETE FROM snapshot WHERE id = %s", (b_id,))
    assert nothing_stored(connection)
    connection.execute("DELETE FROM snapshot WHERE id = %s", (a_id,))
