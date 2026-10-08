"""The HTTP API against a real PostgreSQL holding the golden snapshots.

Skipped unless ``XRAY_TEST_DATABASE_URL`` is set. Snapshots are written through a
separate, writable connection; the API's own sessions are read-only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.analyzer import analyze
from xraymyrepo.api import Settings, create_app
from xraymyrepo.cim import SnapshotDocument
from xraymyrepo.cim.evidence import FactEvidence
from xraymyrepo.persistence import persist_snapshot
from xraymyrepo.persistence import queries as q

from ..conftest import GOLDEN_REPO, PRODUCER_HASH, SHA, reset_database
from ..test_persistence_db import minimal_document

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

API = "/api/v1"
SERVICE = "backend/app/users/service.py#UserService"
ENDPOINT = "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend"
NODE_FIELDS = ("span", "basis", "confidence", "provenance", "language", "blob_sha",
               "extraction_status", "content_hash", "signature_hash", "attributes")  # fmt: skip


@pytest.fixture(scope="module")
def stored(golden: SnapshotDocument) -> dict[str, int]:
    assert psycopg is not None and DATABASE_URL is not None
    analyzed = analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                       commit_sha=golden.snapshot.commit_sha)  # fmt: skip
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        ids = {
            "golden": persist_snapshot(conn, golden),
            "analyzed": persist_snapshot(conn, analyzed),
            "minimal": persist_snapshot(conn, minimal_document()),
        }
        repository = conn.execute("SELECT id FROM repository WHERE name = 'Empty'").fetchone()[0]
        ids["pending"] = conn.execute(
            "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
            "cim_schema_version, config) VALUES (%s, %s, '9.0.0', %s, '1.0', '{}') RETURNING id",
            (repository, SHA, PRODUCER_HASH),
        ).fetchone()[0]
    return ids


@pytest.fixture(scope="module")
def client(stored: dict[str, int]) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def sid(stored: dict[str, int]) -> int:
    return stored["golden"]


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def problem(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert (body["status"], body["code"], body["type"]) == (
        status, code, f"urn:xraymyrepo:problem:{code}")  # fmt: skip
    return body


def all_pages(client: TestClient, path: str, **params: Any) -> list[Any]:
    items: list[Any] = []
    while True:
        page = ok(client.get(path, params=params))
        items += page["items"]
        if page["next_cursor"] is None:
            return items
        params["cursor"] = page["next_cursor"]


def dump_evidence(evidence: Any) -> list[Any]:
    from pydantic import TypeAdapter

    adapter: TypeAdapter[tuple[FactEvidence, ...]] = TypeAdapter(tuple[FactEvidence, ...])
    return list(adapter.dump_python(evidence, mode="json"))


# ---------------------------------------------------------------------------
# Service


def test_ready(client: TestClient) -> None:
    body = ok(client.get("/ready"))
    assert body == {"status": "ready", "applied_migrations": list(q.REQUIRED_MIGRATIONS),
                    "missing_migrations": [], "detail": None}  # fmt: skip


def test_api_sessions_are_read_only(client: TestClient, sid: int) -> None:
    assert psycopg is not None
    pool = client.app.state.pool  # type: ignore[attr-defined]
    with pool.connection() as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("DELETE FROM snapshot WHERE id = %s", (sid,))


# ---------------------------------------------------------------------------
# Repositories and snapshots


def test_repositories(client: TestClient, stored: dict[str, int]) -> None:
    repositories = all_pages(client, f"{API}/repositories", limit=1)
    assert [r["slug"] for r in repositories] == [
        "example.com/Acme/Empty", "github.com/xraymyrepo/golden-fixture"]  # fmt: skip
    fixture = repositories[1]
    assert fixture["snapshot_count"] == 2
    assert fixture["latest_snapshot"]["id"] == stored["analyzed"]
    empty = ok(client.get(f"{API}/repositories/example.com/acme/EMPTY"))
    assert (empty["owner"], empty["name"], empty["snapshot_count"]) == ("Acme", "Empty", 1)
    problem(client.get(f"{API}/repositories/example.com/acme/nope"), 404, "repository_not_found")


def test_snapshots(client: TestClient, stored: dict[str, int], golden: SnapshotDocument) -> None:
    path = f"{API}/repositories/github.com/xraymyrepo/golden-fixture/snapshots"
    listed = all_pages(client, path, limit=1)
    assert [s["id"] for s in listed] == [stored["analyzed"], stored["golden"]]
    found = ok(client.get(path, params={"extractor_version": "1.0.0",
                                        "config_hash": golden.snapshot.config_hash}))  # fmt: skip
    assert [s["id"] for s in found["items"]] == [stored["golden"]]
    pending = ok(client.get(f"{API}/repositories/example.com/Acme/Empty/snapshots",
                            params={"status": "pending"}))  # fmt: skip
    assert [s["id"] for s in pending["items"]] == [stored["pending"]]
    problem(client.get(path, params={"status": "done"}), 422, "validation_error")


def test_snapshot_detail(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    detail = ok(client.get(f"{API}/snapshots/{sid}"))
    assert detail["commit_sha"] == golden.snapshot.commit_sha
    assert detail["repository"] == golden.snapshot.repository.model_dump(mode="json")
    assert detail["config"] == golden.config.model_dump(mode="json")
    assert [p["name"] for p in detail["producers"]] == sorted(p.name for p in golden.producers)
    assert detail["root"]["key"] == "/"
    assert detail["root"]["child_count"] == len(golden.children("/"))
    assert sum(detail["stats"]["nodes_by_kind"].values()) == len(golden.nodes)
    assert detail["stats"]["edges_by_kind"]["IMPORTS"] == 18


def test_snapshot_gate(client: TestClient, stored: dict[str, int]) -> None:
    problem(client.get(f"{API}/snapshots/999999"), 404, "snapshot_not_found")
    for path in ("", "/tree/children", "/nodes", "/search?q=x"):
        problem(client.get(f"{API}/snapshots/{stored['pending']}{path}"), 409,
                "snapshot_not_complete")  # fmt: skip
    problem(client.get(f"{API}/snapshots/abc"), 422, "validation_error")
    problem(client.get(f"{API}/snapshots/0"), 422, "validation_error")


# ---------------------------------------------------------------------------
# Tree and nodes


def test_tree_children(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    root = ok(client.get(f"{API}/snapshots/{sid}/tree/children"))
    assert [n["key"] for n in root["items"]] == ["backend", "frontend", "scripts", "README.md"]
    readme = root["items"][3]
    assert readme["child_count"] == 0 and readme["extraction_status"] == "excluded"
    assert {"facet": "origin", "value": "docs"} in readme["tags"]
    backend = ok(client.get(f"{API}/snapshots/{sid}/tree/children",
                            params={"parent": "backend/app/users/service.py"}))  # fmt: skip
    assert [(n["key"], n["child_count"]) for n in backend["items"]] == [(SERVICE, 2)]


def test_full_tree_through_the_api(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    """Expanding every node, two per page, reaches every tree node exactly once."""
    seen: list[str] = []
    frontier = ["/"]
    while frontier:
        parent = frontier.pop()
        children = all_pages(client, f"{API}/snapshots/{sid}/tree/children", parent=parent,
                             limit=2)  # fmt: skip
        assert [c["key"] for c in children] == [
            n.key for n in sorted(golden.children(parent), key=q.child_sort_key)]  # fmt: skip
        assert all(c["child_count"] == len(golden.children(c["key"])) for c in children)
        seen += [c["key"] for c in children]
        frontier += [c["key"] for c in children if c["child_count"]]
    assert sorted(seen) == sorted(n.key for n in golden.nodes if n.parent_key is not None)


def test_tree_errors(client: TestClient, sid: int) -> None:
    path = f"{API}/snapshots/{sid}/tree/children"
    problem(client.get(path, params={"parent": "no/such/dir"}), 404, "node_not_found")
    body = problem(client.get(path, params={"parent": "a//b"}), 422, "invalid_key")
    assert "parent" in body["detail"]
    problem(client.get(path, params={"cursor": "garbage"}), 400, "invalid_cursor")
    search_cursor = ok(client.get(f"{API}/snapshots/{sid}/search",
                                  params={"q": "user", "limit": 1}))["next_cursor"]  # fmt: skip
    problem(client.get(path, params={"cursor": search_cursor}), 400, "invalid_cursor")
    body = problem(client.get(path, params={"limit": 0}), 422, "validation_error")
    assert body["errors"][0]["loc"] == ["query", "limit"]
    problem(client.get(path, params={"limit": 1001}), 422, "validation_error")
    problem(client.get(path, params={"kind": "group"}), 422, "validation_error")


def test_list_nodes(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    kinds = ["package", "external_package"]
    packages = all_pages(client, f"{API}/snapshots/{sid}/nodes", kind=kinds, limit=4)
    assert [p["key"] for p in packages] == sorted(
        n.key for n in golden.nodes if n.kind in ("package", "external_package"))  # fmt: skip
    endpoints = ok(client.get(f"{API}/snapshots/{sid}/nodes", params={"kind": "endpoint"}))
    assert ENDPOINT in [e["key"] for e in endpoints["items"]]
    prefixed = ok(client.get(f"{API}/snapshots/{sid}/nodes", params={"prefix": "frontend/src/"}))
    assert all(n["key"].startswith("frontend/src/") for n in prefixed["items"])


def test_every_node_through_the_api(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    """Node detail carries the CIM node unchanged; keys with '/', '#', ':', '{}', '@' and
    spaces travel as encoded query parameters."""
    for node in golden.nodes:
        detail = ok(client.get(f"{API}/snapshots/{sid}/nodes/by-key", params={"key": node.key}))
        assert (detail["key"], detail["kind"], detail["name"], detail["parent_key"]) == (
            node.key, node.kind, node.name, node.parent_key)  # fmt: skip
        cim = node.model_dump(mode="json")
        for field in NODE_FIELDS:
            assert detail[field] == cim[field], (node.key, field)
        assert detail["evidence"] == dump_evidence(node.evidence)
        classifications = golden.classifications_of(node.key)
        assert sorted((c["facet"], c["value"]) for c in detail["classifications"]) == sorted(
            (c.facet.value, c.value) for c in classifications)  # fmt: skip
        assert detail["child_count"] == len(golden.children(node.key))


def test_node_detail(client: TestClient, sid: int) -> None:
    detail = ok(client.get(f"{API}/snapshots/{sid}/nodes/by-key", params={"key": SERVICE}))
    assert [a["key"] for a in detail["ancestors"]] == [
        "/", "backend", "backend/app", "backend/app/users", "backend/app/users/service.py",
    ]  # fmt: skip
    assert detail["attributes"] == {"type_kind": "class"}
    assert detail["relationship_counts"]["IMPORTS"] == {"outgoing": 0, "incoming": 2}
    assert detail["diagnostic_counts"] == {"unresolved_references": 0, "extraction_issues": 0}
    endpoint = ok(client.get(f"{API}/snapshots/{sid}/nodes/by-key", params={"key": ENDPOINT}))
    assert endpoint["attributes"]["route"] == "/users/{user_id}"
    path = f"{API}/snapshots/{sid}/nodes/by-key"
    problem(client.get(path, params={"key": "backend/missing.py"}), 404, "node_not_found")
    problem(client.get(path, params={"key": "pkg:pypi:Not Normal"}), 422, "invalid_key")
    problem(client.get(path), 422, "validation_error")


# ---------------------------------------------------------------------------
# Relationships and edges


def test_relationships_keep_stored_direction(client: TestClient, sid: int,
                                             golden: SnapshotDocument) -> None:  # fmt: skip
    incoming = ok(client.get(f"{API}/snapshots/{sid}/relationships",
                             params={"node": SERVICE, "direction": "in"}))["items"]  # fmt: skip
    assert incoming and all(r["direction"] == "in" and r["target_key"] == SERVICE for r in incoming)
    assert {(r["kind"], r["source_key"]) for r in incoming} == {
        (e.kind.value, e.source_key) for e in golden.edges if e.target_key == SERVICE}  # fmt: skip
    assert all(r["peer"]["key"] == r["source_key"] for r in incoming)
    imports = ok(client.get(f"{API}/snapshots/{sid}/relationships",
                            params={"node": SERVICE, "kind": "IMPORTS"}))["items"]  # fmt: skip
    assert {r["kind"] for r in imports} == {"IMPORTS"}


def test_every_edge_through_the_api(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    found: list[tuple[str, str, str]] = []
    for node in golden.nodes:
        items = all_pages(client, f"{API}/snapshots/{sid}/relationships", node=node.key,
                          direction="out", limit=2)  # fmt: skip
        found += [(r["kind"], r["source_key"], r["target_key"]) for r in items]
    assert sorted(found) == sorted((e.kind.value, e.source_key, e.target_key) for e in golden.edges)


def test_edge_detail(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    for e in golden.edges:
        detail = ok(client.get(f"{API}/snapshots/{sid}/edges/by-ref",
                               params={"kind": e.kind.value, "source": e.source_key,
                                       "target": e.target_key}))  # fmt: skip
        assert detail["evidence"] == dump_evidence(e.evidence)
        assert detail["attributes"] == e.attributes.model_dump(mode="json")
        assert (detail["source"]["key"], detail["target"]["key"]) == (e.source_key, e.target_key)
    e = golden.edges[0]
    problem(client.get(f"{API}/snapshots/{sid}/edges/by-ref",
                       params={"kind": e.kind.value, "source": e.target_key,
                               "target": e.source_key}), 404, "edge_not_found")  # fmt: skip
    problem(client.get(f"{API}/snapshots/{sid}/edges/by-ref",
                       params={"kind": "DEPENDS_ON", "source": "/", "target": "/"}),
            422, "validation_error")  # fmt: skip


# ---------------------------------------------------------------------------
# Search and diagnostics


def test_search(client: TestClient, sid: int) -> None:
    hits = ok(client.get(f"{API}/snapshots/{sid}/search", params={"q": "UserService"}))["items"]
    assert (hits[0]["node"]["key"], hits[0]["matched"]) == (SERVICE, "name")
    paths = ok(client.get(f"{API}/snapshots/{sid}/search", params={"q": "users/ser"}))["items"]
    assert paths and {h["matched"] for h in paths} == {"key"}
    paged = all_pages(client, f"{API}/snapshots/{sid}/search", q="user", limit=2)
    full = ok(client.get(f"{API}/snapshots/{sid}/search", params={"q": "user"}))["items"]
    assert paged == full
    problem(client.get(f"{API}/snapshots/{sid}/search"), 422, "validation_error")
    problem(client.get(f"{API}/snapshots/{sid}/search", params={"q": ""}), 422,
            "validation_error")  # fmt: skip


def test_diagnostics(client: TestClient, sid: int, golden: SnapshotDocument) -> None:
    plugins = ok(client.get(f"{API}/snapshots/{sid}/diagnostics",
                            params={"node": "backend/app/plugins.py"}))  # fmt: skip
    assert plugins["node"]["key"] == "backend/app/plugins.py"
    assert len(plugins["unresolved_references"]) == 2
    legacy = ok(client.get(f"{API}/snapshots/{sid}/diagnostics",
                           params={"node": "backend/app/legacy.py"}))  # fmt: skip
    assert [i["code"] for i in legacy["extraction_issues"]] == [
        i.code for i in golden.extraction_issues]  # fmt: skip
    problem(client.get(f"{API}/snapshots/{sid}/diagnostics", params={"node": "x.py"}), 404,
            "node_not_found")  # fmt: skip


# ---------------------------------------------------------------------------
# Failures and query budget


def test_internal_errors_disclose_nothing(stored: dict[str, int],
                                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    assert DATABASE_URL is not None

    def broken(*args: Any) -> None:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(q, "get_snapshot", broken)
    app = create_app(Settings(database_url=DATABASE_URL))
    with TestClient(app, raise_server_exceptions=False) as client:
        body = problem(client.get(f"{API}/snapshots/{stored['golden']}"), 500, "internal_error")
    assert "secret" not in str(body)


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every SQL statement executed in the process while the test runs."""
    assert psycopg is not None
    executed: list[str] = []
    original = psycopg.Cursor.execute

    def counting(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(query))
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counting)
    return executed


BUDGET = [
    ("/repositories", {}, 1),
    ("/repositories/github.com/xraymyrepo/golden-fixture/snapshots", {}, 2),
    ("/snapshots/{sid}", {}, 6),
    ("/snapshots/{sid}/tree/children", {"parent": "backend/app"}, 5),
    ("/snapshots/{sid}/nodes", {"kind": "external_package"}, 4),
    ("/snapshots/{sid}/nodes/by-key", {"key": SERVICE}, 7),
    ("/snapshots/{sid}/relationships", {"node": SERVICE}, 3),
    ("/snapshots/{sid}/edges/by-ref", {"kind": "TESTS", "source": "backend/tests/test_users.py",
                                       "target": SERVICE}, 6),
    ("/snapshots/{sid}/search", {"q": "user"}, 4),
    ("/snapshots/{sid}/diagnostics", {"node": "backend/app/plugins.py"}, 4),
]  # fmt: skip


@pytest.mark.parametrize(("path", "params", "budget"), BUDGET)
def test_query_budget(client: TestClient, sid: int, statements: list[str], path: str,
                      params: dict[str, Any], budget: int) -> None:  # fmt: skip
    """A fixed number of statements per request, whatever the page size (no N+1)."""
    url = API + path.format(sid=sid)
    for limit in (1, 1000):
        statements.clear()
        ok(client.get(url, params={**params, "limit": limit} if "by-key" not in path
                      and "by-ref" not in path and "diagnostics" not in path
                      and path != "/snapshots/{sid}" else params))  # fmt: skip
        assert len(statements) == budget, statements
