"""Applies the migration to a real PostgreSQL and loads the golden document into it.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database
and ``psycopg`` is installed (``pip install -e .[db-tests]``). The loader here is
a test helper that checks the schema accepts exactly what the contract produces;
it is not the product's ingestion path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from xraymyrepo.cim import Node, SnapshotDocument
from xraymyrepo.cim.snapshot import CIM_SCHEMA_VERSION

from .conftest import MIGRATIONS

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
        yield connection


def _dump(items: Any) -> str:
    return json.dumps(items)


def load(conn: Any, doc: SnapshotDocument) -> int:
    """Insert a snapshot document; returns the snapshot id. Leaves the snapshot complete."""
    snap = doc.snapshot
    with conn.transaction():
        repo_id = conn.execute(
            "INSERT INTO repository (host, owner, name) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (snap.repository.host, snap.repository.owner, snap.repository.name),
        ).fetchone()
        if repo_id is None:
            repo_id = conn.execute(
                "SELECT id FROM repository WHERE host = %s AND lower(owner) = lower(%s) "
                "AND lower(name) = lower(%s)",
                (snap.repository.host, snap.repository.owner, snap.repository.name),
            ).fetchone()
        snapshot_id = conn.execute(
            "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
            "cim_schema_version, config, coverage) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (repo_id[0], snap.commit_sha, snap.extractor_version, snap.config_hash,
             CIM_SCHEMA_VERSION, _dump(doc.config.model_dump(mode="json")),
             _dump([c.model_dump(mode="json") for c in doc.coverage])),
        ).fetchone()[0]  # fmt: skip

        producers: dict[str, int] = {}
        for p in doc.producers:
            row = conn.execute(
                "INSERT INTO producer (name, version, config_hash) VALUES (%s, %s, %s) "
                "ON CONFLICT (name, version, config_hash) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                (p.name, p.version, p.config_hash),
            ).fetchone()
            producers[p.name] = row[0]
            conn.execute(
                "INSERT INTO snapshot_producer VALUES (%s, %s, %s)",
                (snapshot_id, row[0], p.name),
            )

        ids: dict[str, int] = {}

        def depth(node: Node) -> int:
            d, current = 0, node
            while current.parent_key is not None:
                d, current = d + 1, doc.node(current.parent_key)
            return d

        def file_of(node: Node) -> str:
            current = node
            while current.kind != "file":
                assert current.parent_key is not None
                current = doc.node(current.parent_key)
            return current.key

        for node in sorted(doc.nodes, key=depth):
            span = node.span
            ids[node.key] = conn.execute(
                "INSERT INTO node (snapshot_id, key, kind, name, parent_id, depth, file_id, "
                "language, start_line, start_col, end_line, end_col, basis, confidence, "
                "producer_id, rule, extraction_status, blob_sha, content_hash, signature_hash, "
                "evidence, attributes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (snapshot_id, node.key, node.kind, node.name,
                 ids.get(node.parent_key or ""), depth(node),
                 ids[file_of(node)] if node.kind in ("type", "function", "endpoint") else None,
                 node.language,
                 span.start_line if span else None, span.start_col if span else None,
                 span.end_line if span else None, span.end_col if span else None,
                 node.basis, node.confidence, producers[node.provenance.producer],
                 node.provenance.rule, node.extraction_status, node.blob_sha,
                 node.content_hash, node.signature_hash,
                 _dump([e.model_dump(mode="json") for e in node.evidence]),
                 node.attributes.model_dump_json()),
            ).fetchone()[0]  # fmt: skip

        for e in doc.edges:
            conn.execute(
                "INSERT INTO edge (snapshot_id, kind, source_id, target_id, basis, confidence, "
                "occurrence_count, ambiguity_group, contested, evidence, attributes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (snapshot_id, e.kind, ids[e.source_key], ids[e.target_key], e.basis,
                 e.confidence, e.occurrence_count, e.ambiguity_group, e.contested,
                 _dump([x.model_dump(mode="json") for x in e.evidence]),
                 e.attributes.model_dump_json()),
            )  # fmt: skip
        for c in doc.classifications:
            conn.execute(
                "INSERT INTO classification (snapshot_id, node_id, facet, value, basis, "
                "confidence, contested, evidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (snapshot_id, ids[c.node_key], c.facet, c.value, c.basis, c.confidence,
                 c.contested, _dump([x.model_dump(mode="json") for x in c.evidence])),
            )  # fmt: skip
        for u in doc.unresolved_references:
            conn.execute(
                "INSERT INTO unresolved_reference (snapshot_id, source_id, file_id, ref_kind, "
                "raw_text, start_line, start_col, end_line, end_col, reason, candidates, "
                "producer_id, rule) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (snapshot_id, ids[u.source_key], ids[u.file_key], u.ref_kind, u.raw_text,
                 u.start_line, u.start_col, u.end_line, u.end_col, u.reason,
                 _dump(list(u.candidates)), producers[u.provenance.producer], u.provenance.rule),
            )  # fmt: skip
        for i in doc.extraction_issues:
            s = i.span
            conn.execute(
                "INSERT INTO extraction_issue (snapshot_id, file_id, severity, code, message, "
                "start_line, start_col, end_line, end_col, producer_id, rule) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (snapshot_id, ids[i.file_key] if i.file_key else None, i.severity, i.code,
                 i.message, s.start_line if s else None, s.start_col if s else None,
                 s.end_line if s else None, s.end_col if s else None,
                 producers[i.provenance.producer], i.provenance.rule),
            )  # fmt: skip
        conn.execute(
            "UPDATE snapshot SET status = 'complete', finished_at = now() WHERE id = %s",
            (snapshot_id,),
        )
    return int(snapshot_id)


@pytest.fixture(scope="module")
def snapshot_id(conn: Any, golden: SnapshotDocument) -> int:
    return load(conn, golden)


def test_golden_loads_completely(conn: Any, snapshot_id: int, golden: SnapshotDocument) -> None:
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM node WHERE snapshot_id = %(s)s), "
        "(SELECT count(*) FROM edge WHERE snapshot_id = %(s)s), "
        "(SELECT count(*) FROM classification WHERE snapshot_id = %(s)s), "
        "(SELECT count(*) FROM unresolved_reference WHERE snapshot_id = %(s)s), "
        "(SELECT count(*) FROM extraction_issue WHERE snapshot_id = %(s)s)",
        {"s": snapshot_id},
    ).fetchone()
    assert counts == (
        len(golden.nodes), len(golden.edges), len(golden.classifications),
        len(golden.unresolved_references), len(golden.extraction_issues),
    )  # fmt: skip
    assert conn.execute("SELECT version FROM schema_migration").fetchall() == [
        ("0001_cim_v1_snapshot_tier",)
    ]


def test_subtree_prefix_query(conn: Any, snapshot_id: int) -> None:
    keys = [r[0] for r in conn.execute(
        "SELECT key FROM node WHERE snapshot_id = %s AND key LIKE 'backend/app/users/%%' "
        "ORDER BY key", (snapshot_id,)
    )]  # fmt: skip
    assert "backend/app/users/service.py#UserService.get" in keys
    assert all(k.startswith("backend/app/users/") for k in keys)


def test_reverse_traversal(conn: Any, snapshot_id: int) -> None:
    importers = {r[0] for r in conn.execute(
        "SELECT s.key FROM edge e "
        "JOIN node t ON (t.snapshot_id, t.id) = (e.snapshot_id, e.target_id) "
        "JOIN node s ON (s.snapshot_id, s.id) = (e.snapshot_id, e.source_id) "
        "WHERE e.snapshot_id = %s AND t.key = %s",
        (snapshot_id, "backend/app/users/service.py#UserService"),
    )}  # fmt: skip
    assert importers == {"backend/app/users/routes.py", "backend/tests/test_users.py"}


def _rejects(conn: Any, sql: str, params: tuple[Any, ...]) -> None:
    assert psycopg is not None
    with pytest.raises(psycopg.errors.IntegrityError), conn.transaction():
        conn.execute(sql, params)


def test_completed_snapshot_is_immutable(conn: Any, snapshot_id: int) -> None:
    _rejects(conn, "UPDATE snapshot SET commit_sha = %s WHERE id = %s", ("f" * 40, snapshot_id))
    _rejects(conn, "UPDATE snapshot SET status = 'failed', failure_reason = 'x' WHERE id = %s",
             (snapshot_id,))  # fmt: skip


def test_identity_is_unique(conn: Any, snapshot_id: int) -> None:
    _rejects(
        conn,
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config) SELECT repository_id, commit_sha, extractor_version, "
        "config_hash, cim_schema_version, config FROM snapshot WHERE id = %s",
        (snapshot_id,),
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [("kind", "group"), ("kind", "module"), ("basis", "interpreted"), ("basis", "inferred"),
     ("confidence", "0.71")],
)  # fmt: skip
def test_node_constraints(conn: Any, snapshot_id: int, column: str, value: str) -> None:
    _rejects(
        conn,
        f"UPDATE node SET {column} = %s WHERE snapshot_id = %s AND key = 'backend/app/db.py#Base'",
        (value, snapshot_id),
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [("kind", "DEPENDS_ON"), ("kind", "DECLARES"), ("basis", "interpreted"),
     ("basis", "resolved"), ("evidence", "[]"), ("ambiguity_group", "g")],
)  # fmt: skip
def test_edge_constraints(conn: Any, snapshot_id: int, column: str, value: str) -> None:
    _rejects(
        conn,
        f"UPDATE edge SET {column} = %s WHERE snapshot_id = %s AND kind = 'TESTS'",
        (value, snapshot_id),
    )


@pytest.mark.parametrize(
    ("kind", "basis"),
    [("REQUIRES", "heuristic"), ("IMPORTS", "observed"), ("INHERITS", "observed"),
     ("HANDLES", "resolved")],
)  # fmt: skip
def test_edge_basis_is_restricted_per_kind(
    conn: Any, snapshot_id: int, kind: str, basis: str
) -> None:
    _rejects(
        conn,
        "UPDATE edge SET basis = %s, confidence = 'high' WHERE snapshot_id = %s AND kind = %s",
        (basis, snapshot_id, kind),
    )


def test_classification_constraints(conn: Any, snapshot_id: int) -> None:
    _rejects(
        conn,
        "UPDATE classification SET value = 'data_model' WHERE snapshot_id = %s AND facet = 'role'",
        (snapshot_id,),
    )


def test_file_columns_required(conn: Any, snapshot_id: int) -> None:
    _rejects(
        conn,
        "UPDATE node SET extraction_status = NULL WHERE snapshot_id = %s AND kind = 'file'",
        (snapshot_id,),
    )
