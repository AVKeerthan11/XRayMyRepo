"""Migration 0004 (snapshot comparison) on a real PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database
(the schema is dropped and recreated). Comparisons are computed by the pure algorithm
and written with a raw-SQL test loader: this checks that the schema accepts exactly
what the contract produces and rejects what it forbids. It is not the product's
persistence path (that is ``xraymyrepo.persistence``).
"""

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Confidence,
    NodeChangeKind,
    RepositoryRef,
    SnapshotComparison,
    SnapshotDocument,
)
from xraymyrepo.cim.snapshot import CIM_SCHEMA_VERSION
from xraymyrepo.insights.comparison import compare
from xraymyrepo.persistence import persist_snapshot
from xraymyrepo.persistence.queries import REQUIRED_MIGRATIONS

from .conftest import MIGRATIONS, ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
_hashes = itertools.count(1)


def fresh_hash() -> str:
    """A params hash no other comparison in this module uses (distinct identities)."""
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
        "other_repo": with_identity(
            after, repository={"host": "github.com", "owner": "xraymyrepo", "name": "elsewhere"}
        ),
    }


@pytest.fixture(scope="module")
def ids(conn: Any, docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    found = {name: persist_snapshot(conn, doc) for name, doc in docs.items()}
    after = docs["after"].snapshot
    found["pending"] = conn.execute(
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config) SELECT repository_id, %s, extractor_version, config_hash, "
        "%s, config FROM snapshot WHERE id = %s RETURNING id",
        ("4" * 40, CIM_SCHEMA_VERSION, found["after"]),
    ).fetchone()[0]
    assert after.commit_sha != "4" * 40
    return found


@pytest.fixture(scope="module")
def fixture_comparison(docs: dict[str, SnapshotDocument]) -> SnapshotComparison:
    return compare(docs["before"], docs["after"])


def node_ids(conn: Any, snapshot_id: int) -> dict[str, int]:
    rows = conn.execute("SELECT key, id FROM node WHERE snapshot_id = %s", (snapshot_id,))
    return dict(rows.fetchall())


def edge_ids(conn: Any, snapshot_id: int) -> dict[tuple[str, str, str], int]:
    rows = conn.execute(
        "SELECT e.kind, s.key, t.key, e.id FROM edge e "
        "JOIN node s ON s.snapshot_id = e.snapshot_id AND s.id = e.source_id "
        "JOIN node t ON t.snapshot_id = e.snapshot_id AND t.id = e.target_id "
        "WHERE e.snapshot_id = %s",
        (snapshot_id,),
    ).fetchall()
    return {(r[0], r[1], r[2]): r[3] for r in rows}


def insert_comparison(conn: Any, c: SnapshotComparison, from_id: int, to_id: int,
                      params_hash: str | None = None) -> int:  # fmt: skip
    """The pending comparison row."""
    row = conn.execute(
        "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
        "algorithm_version, params_hash, params, incomparable) "
        "VALUES (%s, %s, %s, %s, %s, '{}', %s) RETURNING id",
        (from_id, to_id, c.run.algorithm, c.run.algorithm_version,
         params_hash or fresh_hash(), json.dumps([r.value for r in c.run.incomparable])),
    ).fetchone()  # fmt: skip
    return int(row[0])


def node_rows(conn: Any, cid: int, c: SnapshotComparison, from_id: int,
              to_id: int) -> list[dict[str, Any]]:  # fmt: skip
    a, b = node_ids(conn, from_id), node_ids(conn, to_id)
    return [
        {
            "comparison_id": cid, "from_snapshot_id": from_id, "to_snapshot_id": to_id,
            "key": n.key, "change": n.change.value, "kind": n.kind.value,
            "from_node_id": None if n.from_key is None else a[n.from_key],
            "to_node_id": None if n.to_key is None else b[n.to_key],
            "match_method": None if n.match_method is None else n.match_method.value,
            "confidence": n.confidence.value, "move_ambiguous": n.move_ambiguous,
        }
        for n in c.nodes
    ]  # fmt: skip


def edge_rows(conn: Any, cid: int, c: SnapshotComparison, from_id: int,
              to_id: int) -> list[dict[str, Any]]:  # fmt: skip
    a, b = edge_ids(conn, from_id), edge_ids(conn, to_id)

    def key(ref: Any) -> tuple[str, str, str]:
        return (ref.kind.value, ref.source_key, ref.target_key)

    return [
        {
            "comparison_id": cid, "from_snapshot_id": from_id, "to_snapshot_id": to_id,
            "change": e.change.value, "kind": e.kind.value,
            "from_edge_id": None if e.from_ref is None else a[key(e.from_ref)],
            "to_edge_id": None if e.to_ref is None else b[key(e.to_ref)],
            "source_key": e.ref.source_key, "target_key": e.ref.target_key,
            "confidence": e.confidence.value,
        }
        for e in c.edges
    ]  # fmt: skip


def insert_node_rows(conn: Any, rows: list[dict[str, Any]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO node_change (comparison_id, from_snapshot_id, to_snapshot_id, key, "
            "change, kind, from_node_id, to_node_id, match_method, confidence, move_ambiguous) "
            "VALUES "
            "(%(comparison_id)s, %(from_snapshot_id)s, %(to_snapshot_id)s, %(key)s, %(change)s, "
            "%(kind)s, %(from_node_id)s, %(to_node_id)s, %(match_method)s, %(confidence)s, "
            "%(move_ambiguous)s)",
            rows,
        )


def insert_edge_rows(conn: Any, rows: list[dict[str, Any]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO edge_change (comparison_id, from_snapshot_id, to_snapshot_id, change, "
            "kind, from_edge_id, to_edge_id, source_key, target_key, confidence) VALUES "
            "(%(comparison_id)s, %(from_snapshot_id)s, %(to_snapshot_id)s, %(change)s, %(kind)s, "
            "%(from_edge_id)s, %(to_edge_id)s, %(source_key)s, %(target_key)s, %(confidence)s)",
            rows,
        )


def summary_json(c: SnapshotComparison) -> str:
    return json.dumps(c.summary().model_dump(mode="json"))


def complete(conn: Any, cid: int, summary: str) -> None:
    conn.execute(
        "UPDATE snapshot_comparison SET status = 'complete', finished_at = now(), summary = %s "
        "WHERE id = %s",
        (summary, cid),
    )


def store(conn: Any, c: SnapshotComparison, from_id: int, to_id: int) -> int:
    """Write a whole comparison and complete it, in one transaction."""
    with conn.transaction():
        cid = insert_comparison(conn, c, from_id, to_id)
        insert_node_rows(conn, node_rows(conn, cid, c, from_id, to_id))
        insert_edge_rows(conn, edge_rows(conn, cid, c, from_id, to_id))
        complete(conn, cid, summary_json(c))
    return cid


def rejected(conn: Any, match: str, error: str = "IntegrityError") -> Any:
    """Context: the transaction inside fails with ``error`` whose message matches."""
    assert psycopg is not None
    return pytest.raises(getattr(psycopg.errors, error), match=match)


def count(conn: Any, table: str, cid: int) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table} WHERE comparison_id = %s", (cid,))
    return int(row.fetchone()[0])


# ---------------------------------------------------------------------------
# Applying and storing


def test_migrations_are_registered(conn: Any) -> None:
    files = tuple(p.stem for p in sorted(MIGRATIONS.glob("*.sql")))
    assert files == REQUIRED_MIGRATIONS and "0004_snapshot_comparison" in files
    applied = conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
    assert tuple(r[0] for r in applied) == REQUIRED_MIGRATIONS


def test_a_comparison_is_stored_complete(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    c = fixture_comparison
    cid = store(conn, c, ids["before"], ids["after"])
    status, summary, incomparable = conn.execute(
        "SELECT status, summary, incomparable FROM snapshot_comparison WHERE id = %s", (cid,)
    ).fetchone()
    assert (status, incomparable) == ("complete", [])
    assert summary == c.summary().model_dump(mode="json")
    # Every node row, unchanged included; only changed or undetermined edges.
    assert count(conn, "node_change", cid) == len(c.nodes) == 33
    assert count(conn, "edge_change", cid) == len(c.edges) == 4
    assert summary["edges"]["unchanged"] == c.unchanged_edges == 1
    undetermined = conn.execute(
        "SELECT from_edge_id IS NOT NULL, to_edge_id IS NOT NULL, confidence FROM edge_change "
        "WHERE comparison_id = %s AND change = 'undetermined'",
        (cid,),
    ).fetchall()
    assert undetermined == [(True, False, "low")]


def test_an_incomparable_comparison_is_stored_with_its_reasons(
    conn: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    c = compare(docs["before"], docs["other_version"])
    cid = store(conn, c, ids["before"], ids["other_version"])
    row = conn.execute("SELECT incomparable FROM snapshot_comparison WHERE id = %s", (cid,))
    assert row.fetchone()[0] == ["extractor_version_differs"]
    high = conn.execute(
        "SELECT count(*) FROM node_change WHERE comparison_id = %s AND confidence = 'high'", (cid,)
    ).fetchone()[0]
    assert high == 0


def test_a_comparison_can_fail(conn: Any, ids: dict[str, int],
                               fixture_comparison: SnapshotComparison) -> None:  # fmt: skip
    cid = insert_comparison(conn, fixture_comparison, ids["before"], ids["after"])
    conn.execute(
        "UPDATE snapshot_comparison SET status = 'failed', failure_reason = 'boom', "
        "finished_at = now() WHERE id = %s",
        (cid,),
    )
    with rejected(conn, "only pending comparisons can transition"), conn.transaction():
        complete(conn, cid, summary_json(fixture_comparison))


# ---------------------------------------------------------------------------
# Which snapshot pairs can be compared


def test_two_different_snapshots(conn: Any, ids: dict[str, int],
                                 fixture_comparison: SnapshotComparison) -> None:  # fmt: skip
    with (
        rejected(conn, "snapshot_comparison_distinct_check", "CheckViolation"),
        conn.transaction(),
    ):
        insert_comparison(conn, fixture_comparison, ids["before"], ids["before"])


def test_of_one_repository(conn: Any, ids: dict[str, int],
                           fixture_comparison: SnapshotComparison) -> None:  # fmt: skip
    with rejected(conn, "different repositories"), conn.transaction():
        insert_comparison(conn, fixture_comparison, ids["before"], ids["other_repo"])


def test_both_complete(conn: Any, ids: dict[str, int],
                       fixture_comparison: SnapshotComparison) -> None:  # fmt: skip
    for pair in ((ids["before"], ids["pending"]), (ids["pending"], ids["before"])):
        with rejected(conn, "must both exist and be complete"), conn.transaction():
            insert_comparison(conn, fixture_comparison, *pair)


def test_incomparable_is_exactly_how_the_identities_differ(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    # The fixture comparison says comparable; these snapshots differ in extractor version.
    with rejected(conn, "incomparable must be"), conn.transaction():
        insert_comparison(conn, fixture_comparison, ids["before"], ids["other_version"])
    # The reasons in another order: the insert trigger (it runs before the CHECK, which
    # lists the allowed values) rejects it too.
    with (
        rejected(conn, "incomparable must be"),
        conn.transaction(),
    ):
        conn.execute(
            "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
            "algorithm_version, params_hash, params, incomparable) VALUES "
            "(%s, %s, 'comparison', '1.0.0', %s, '{}', '[\"config_differs\", "
            '"extractor_version_differs"]\')',
            (ids["before"], ids["other_version"], fresh_hash()),
        )


def test_identity_is_unique(conn: Any, ids: dict[str, int],
                            fixture_comparison: SnapshotComparison) -> None:  # fmt: skip
    params_hash = fresh_hash()
    insert_comparison(conn, fixture_comparison, ids["after"], ids["before"], params_hash)
    with (
        rejected(conn, "snapshot_comparison_identity_key", "UniqueViolation"),
        conn.transaction(),
    ):
        insert_comparison(conn, fixture_comparison, ids["after"], ids["before"], params_hash)


# ---------------------------------------------------------------------------
# Completed comparisons are immutable


@pytest.fixture
def stored(conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison) -> int:
    return store(conn, fixture_comparison, ids["before"], ids["after"])


@pytest.mark.parametrize(
    "assignment",
    [
        "params_hash = 'sha256:" + "f" * 64 + "'",
        "incomparable = '[\"config_differs\"]'",
        "status = 'failed', failure_reason = 'x'",
        "summary = summary",
        "created_at = now() - interval '1 day'",
    ],
)
def test_a_complete_comparison_rejects_updates(conn: Any, stored: int, assignment: str) -> None:
    with rejected(conn, "immutable|only pending"), conn.transaction():
        conn.execute(f"UPDATE snapshot_comparison SET {assignment} WHERE id = %s", (stored,))


@pytest.mark.parametrize(
    "statement",
    [
        # Each would be valid row by row; only the comparison's status forbids it.
        "UPDATE node_change SET confidence = confidence WHERE comparison_id = %s",
        "DELETE FROM node_change WHERE comparison_id = %s",
        "UPDATE edge_change SET confidence = confidence WHERE comparison_id = %s",
        "DELETE FROM edge_change WHERE comparison_id = %s",
        # An undetermined row for an unchanged edge of to (not referenced by any row).
        "INSERT INTO edge_change (comparison_id, from_snapshot_id, to_snapshot_id, change, kind, "
        "to_edge_id, source_key, target_key, confidence) "
        "SELECT c.id, c.from_snapshot_id, c.to_snapshot_id, 'undetermined', e.kind, e.id, s.key, "
        "t.key, 'low' FROM snapshot_comparison c "
        "JOIN edge e ON e.snapshot_id = c.to_snapshot_id "
        "JOIN node s ON s.snapshot_id = e.snapshot_id AND s.id = e.source_id "
        "JOIN node t ON t.snapshot_id = e.snapshot_id AND t.id = e.target_id "
        "WHERE c.id = %s AND NOT EXISTS (SELECT 1 FROM edge_change r "
        "WHERE r.comparison_id = c.id AND r.to_edge_id = e.id) LIMIT 1",
    ],
)
def test_rows_of_a_complete_comparison_are_immutable(
    conn: Any, stored: int, statement: str
) -> None:
    with rejected(conn, "rows are immutable"), conn.transaction():
        conn.execute(statement, (stored,))


def test_a_complete_comparison_can_be_deleted_as_a_whole(conn: Any, stored: int) -> None:
    conn.execute("DELETE FROM snapshot_comparison WHERE id = %s", (stored,))
    assert count(conn, "node_change", stored) == count(conn, "edge_change", stored) == 0


# ---------------------------------------------------------------------------
# Row checks (on a pending comparison)


@pytest.fixture
def pending(conn: Any, ids: dict[str, int],
            fixture_comparison: SnapshotComparison) -> dict[str, Any]:  # fmt: skip
    """A pending comparison with one valid row of each table to vary."""
    c = fixture_comparison
    cid = insert_comparison(conn, c, ids["before"], ids["after"])
    nodes = node_rows(conn, cid, c, ids["before"], ids["after"])
    edges = edge_rows(conn, cid, c, ids["before"], ids["after"])
    return {
        "id": cid,
        "added": next(r for r in nodes if r["change"] == "added" and r["kind"] == "function"),
        "unchanged": next(r for r in nodes if r["change"] == "unchanged" and r["kind"] == "file"),
        "added_edge": next(r for r in edges if r["change"] == "added"),
        "removed_edge": next(r for r in edges if r["change"] == "removed"),
    }


@pytest.mark.parametrize(
    ("base", "update", "constraint"),
    [
        ("added", {"change": "renamed"}, "node_change_change_check"),
        ("added", {"kind": "group"}, "node_change_kind_check"),
        ("added", {"confidence": "certain"}, "node_change_confidence_check"),
        ("added", {"match_method": "fuzzy"}, "node_change_match_method_check"),
        ("added", {"from_node_id": "to_node_id"}, "node_change_shape_check"),
        ("added", {"match_method": "same_key"}, "node_change_shape_check"),
        ("added", {"confidence": "low"}, "node_change_change_confidence_check"),
        ("added", {"move_ambiguous": True}, "node_change_move_ambiguous_check"),
        ("unchanged", {"match_method": "same_content_hash"}, "node_change_shape_check"),
        ("unchanged", {"to_node_id": None}, "node_change_shape_check"),
        ("unchanged", {"change": "moved", "match_method": "same_content_hash"},
         "node_change_change_confidence_check"),
        ("unchanged", {"change": "undetermined"}, "node_change_change_confidence_check"),
        ("unchanged", {"change": "undetermined", "confidence": "low", "match_method": None},
         "node_change_shape_check"),
        ("added", {"key": "bad\x01key"}, "node_change_key_check"),
    ],
)  # fmt: skip
def test_node_change_checks(conn: Any, pending: dict[str, Any], base: str,
                            update: dict[str, Any], constraint: str) -> None:  # fmt: skip
    row = dict(pending[base])
    for column, value in update.items():
        row[column] = row.get(value, value)  # a column name copies that column
    with rejected(conn, constraint, "CheckViolation"), conn.transaction():
        insert_node_rows(conn, [row])


@pytest.mark.parametrize(
    ("base", "update", "constraint"),
    [
        ("added_edge", {"change": "unchanged"}, "edge_change_change_check"),
        ("added_edge", {"kind": "USES"}, "edge_change_kind_check"),
        ("added_edge", {"from_edge_id": "_removed"}, "edge_change_shape_check"),
        ("removed_edge", {"to_edge_id": "_added"}, "edge_change_shape_check"),
        ("added_edge", {"change": "modified"}, "edge_change_shape_check"),
        ("added_edge", {"confidence": "low"}, "edge_change_change_confidence_check"),
        ("added_edge", {"change": "undetermined"}, "edge_change_change_confidence_check"),
        ("added_edge", {"change": "undetermined", "confidence": "low", "from_edge_id": "_removed"},
         "edge_change_shape_check"),
        ("added_edge", {"source_key": ""}, "edge_change_keys_check"),
    ],
)  # fmt: skip
def test_edge_change_checks(conn: Any, pending: dict[str, Any], base: str,
                            update: dict[str, Any], constraint: str) -> None:  # fmt: skip
    row = dict(pending[base])
    for column, value in update.items():
        if value == "_removed":
            value = pending["removed_edge"]["from_edge_id"]
        elif value == "_added":
            value = pending["added_edge"]["to_edge_id"]
        row[column] = value
    with rejected(conn, constraint, "CheckViolation"), conn.transaction():
        insert_edge_rows(conn, [row])


def test_undetermined_edges_have_exactly_one_ref_and_low_confidence(
    conn: Any, pending: dict[str, Any]
) -> None:
    for side in ("added_edge", "removed_edge"):
        row = {**pending[side], "change": "undetermined", "confidence": "low"}
        insert_edge_rows(conn, [row])


def test_keys_and_refs_are_unique_per_side(conn: Any, pending: dict[str, Any]) -> None:
    insert_node_rows(conn, [pending["added"]])
    with rejected(conn, "node_change_pkey", "UniqueViolation"), conn.transaction():
        insert_node_rows(conn, [pending["added"]])
    with rejected(conn, "node_change_to_node_key", "UniqueViolation"), conn.transaction():
        insert_node_rows(conn, [{**pending["added"], "key": "other.py#x"}])
    insert_edge_rows(conn, [pending["added_edge"]])
    with rejected(conn, "edge_change_to_edge_key", "UniqueViolation"), conn.transaction():
        insert_edge_rows(conn, [pending["added_edge"]])


# ---------------------------------------------------------------------------
# References: deferred, checked at commit, tied to the comparison's snapshots


def test_rows_reference_nodes_and_edges_of_their_own_snapshots(
    conn: Any, ids: dict[str, int], pending: dict[str, Any]
) -> None:
    assert psycopg is not None
    to_nodes = node_ids(conn, ids["after"])
    missing = {**pending["added"], "to_node_id": max(to_nodes.values()) + 1000}
    with rejected(conn, "node_change_to_node_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_node_rows(conn, [missing])  # deferred: fails at commit
    # A node of the other snapshot does not exist under this snapshot id.
    from_nodes = node_ids(conn, ids["before"])
    wrong_side = {**pending["added"], "to_node_id": from_nodes["shop/legacy.py#old_discount"]}
    with rejected(conn, "node_change_to_node_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_node_rows(conn, [wrong_side])
    # The row's snapshots are the comparison's.
    other = {**pending["added"], "to_snapshot_id": ids["other_version"]}
    with rejected(conn, "node_change_comparison_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_node_rows(conn, [other])
    bad_edge = {**pending["added_edge"], "to_edge_id": 10**9}
    with rejected(conn, "edge_change_to_edge_fkey", "ForeignKeyViolation"), conn.transaction():
        insert_edge_rows(conn, [bad_edge])


# ---------------------------------------------------------------------------
# Completing checks the rows against both snapshots and the summary


def attempt(conn: Any, ids: dict[str, int], c: SnapshotComparison, *,
            nodes: Any = None, edges: Any = None, summary: Any = None,
            to_id: int | None = None) -> None:  # fmt: skip
    """Write ``c`` with its rows/summary transformed, then complete it."""
    to_id = ids["after"] if to_id is None else to_id
    with conn.transaction():
        cid = insert_comparison(conn, c, ids["before"], to_id)
        node_list = node_rows(conn, cid, c, ids["before"], to_id)
        edge_list = edge_rows(conn, cid, c, ids["before"], to_id)
        insert_node_rows(conn, nodes(node_list) if nodes else node_list)
        insert_edge_rows(conn, edges(edge_list) if edges else edge_list)
        data = c.summary().model_dump(mode="json")
        complete(conn, cid, json.dumps(summary(data) if summary else data))


def test_completion_requires_every_node(conn: Any, ids: dict[str, int],
                                       fixture_comparison: SnapshotComparison) -> None:  # fmt: skip
    c = fixture_comparison
    dropped = c.model_copy(update={"nodes": c.nodes[1:]})  # the repository node's row
    with rejected(conn, "do not cover both snapshots"):
        attempt(conn, ids, dropped)


def test_completion_requires_rows_to_match_their_nodes(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    def rekey(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**r, "key": r["key"] + "-x"} if r["key"] == "shop/catalog.py" else r for r in rows]

    with rejected(conn, "does not match its nodes"):
        attempt(conn, ids, fixture_comparison, nodes=rekey)

    def fake_move(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """legacy.py (removed) and catalog.py (added) as one 'moved' file: different blobs."""
        legacy = next(r for r in rows if r["key"] == "shop/legacy.py")
        catalog = next(r for r in rows if r["key"] == "shop/catalog.py")
        moved = {**catalog, "change": "moved", "from_node_id": legacy["from_node_id"],
                 "match_method": "same_content_hash", "confidence": "medium"}  # fmt: skip
        return [moved if r is catalog else r for r in rows if r is not legacy]

    def recount(summary: dict[str, Any]) -> dict[str, Any]:
        nodes = summary["nodes"]
        nodes.update(moved=nodes["moved"] + 1, added=nodes["added"] - 1,
                     removed=nodes["removed"] - 1)  # fmt: skip
        return summary

    with rejected(conn, "does not match its nodes"):
        attempt(conn, ids, fixture_comparison, nodes=fake_move, summary=recount)


def test_completion_requires_edge_rows_to_match_their_edges(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    def retarget(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**r, "target_key": "shop/util.py"} if r["change"] == "added" else r for r in rows]

    with rejected(conn, "does not match its edges"):
        attempt(conn, ids, fixture_comparison, edges=retarget)


def test_completion_requires_the_summary_to_count_the_rows(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    def off_by_one(key: str, change: str) -> Any:
        def change_summary(summary: dict[str, Any]) -> dict[str, Any]:
            summary[key][change] += 1
            return summary

        return change_summary

    with rejected(conn, "the summary does not count the rows"):
        attempt(conn, ids, fixture_comparison, summary=off_by_one("nodes", "unchanged"))
    with rejected(conn, "the summary does not count the rows"):
        attempt(conn, ids, fixture_comparison, summary=off_by_one("edges", "undetermined"))
    with rejected(conn, "do not account for every edge"):
        attempt(conn, ids, fixture_comparison, summary=off_by_one("edges", "unchanged"))


@pytest.mark.parametrize(
    "summary",
    [
        lambda s: {"nodes": s["nodes"]},  # no edges
        lambda s: {**s, "extra": {}},
        lambda s: {**s, "nodes": {k: v for k, v in s["nodes"].items() if k != "moved"}},
        lambda s: {**s, "edges": {**s["edges"], "unchanged": 1.5}},
        lambda s: {**s, "edges": {**s["edges"], "unchanged": -1}},
        lambda s: {**s, "nodes": {**s["nodes"], "added": "11"}},
    ],
)
def test_the_summary_has_every_count(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison, summary: Any
) -> None:
    with rejected(conn, "snapshot_comparison_summary_check", "CheckViolation"):
        attempt(conn, ids, fixture_comparison, summary=summary)


def test_a_complete_comparison_has_a_summary(
    conn: Any, ids: dict[str, int], fixture_comparison: SnapshotComparison
) -> None:
    cid = insert_comparison(conn, fixture_comparison, ids["before"], ids["after"])
    with (
        rejected(conn, "snapshot_comparison_summary_required_check", "CheckViolation"),
        conn.transaction(),
    ):
        conn.execute(
            "UPDATE snapshot_comparison SET status = 'complete', finished_at = now() WHERE id = %s",
            (cid,),
        )


def test_completion_caps_confidence_when_not_comparable(
    conn: Any, ids: dict[str, int], docs: dict[str, SnapshotDocument]
) -> None:
    c = compare(docs["before"], docs["other_version"])

    def raise_one(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        first = next(r for r in rows if r["change"] == "unchanged")
        return [{**r, "confidence": "high"} if r is first else r for r in rows]

    with rejected(conn, "not fully comparable"):
        attempt(conn, ids, c, nodes=raise_one, to_id=ids["other_version"])
    assert all(n.confidence is not Confidence.HIGH for n in c.nodes)


# ---------------------------------------------------------------------------
# Deleting either snapshot cascades through its comparisons


def test_deleting_either_snapshot_removes_its_comparisons(
    conn: Any, docs: dict[str, SnapshotDocument]
) -> None:
    a = persist_snapshot(conn, with_identity(docs["before"], commit_sha="a" * 40))
    b = persist_snapshot(conn, with_identity(docs["after"], commit_sha="b" * 40))
    c = persist_snapshot(conn, with_identity(docs["after"], commit_sha="c" * 40))
    before_a = with_identity(docs["before"], commit_sha="a" * 40)
    after_b = with_identity(docs["after"], commit_sha="b" * 40)
    after_c = with_identity(docs["after"], commit_sha="c" * 40)
    forward = store(conn, compare(before_a, after_b), a, b)  # a is from
    backward = store(conn, compare(after_b, before_a), b, a)  # a is to
    kept = store(conn, compare(after_b, after_c), b, c)
    assert all(n.change is NodeChangeKind.UNCHANGED for n in compare(after_b, after_c).nodes)

    conn.execute("DELETE FROM snapshot WHERE id = %s", (a,))
    remaining = {r[0] for r in conn.execute("SELECT id FROM snapshot_comparison").fetchall()}
    assert forward not in remaining and backward not in remaining and kept in remaining
    for cid in (forward, backward):
        assert count(conn, "node_change", cid) == count(conn, "edge_change", cid) == 0
    assert count(conn, "node_change", kept) == len(compare(after_b, after_c).nodes)

    conn.execute("DELETE FROM snapshot WHERE id = %s", (c,))  # the to side
    assert conn.execute("SELECT count(*) FROM snapshot_comparison WHERE id = %s",
                        (kept,)).fetchone()[0] == 0  # fmt: skip
    assert count(conn, "node_change", kept) == 0
    assert conn.execute("SELECT count(*) FROM node WHERE snapshot_id = %s", (b,)).fetchone()[0]
