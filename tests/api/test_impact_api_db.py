"""Change impact against PostgreSQL: database-loaded graphs, the on-request API, the
per-process graph cache, lens mapping and query budgets.

Skipped unless ``XRAY_TEST_DATABASE_URL`` is set.
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
from xraymyrepo.graph import DependencyGraph
from xraymyrepo.insights.impact import ImpactParams, impact

from ..conftest import GOLDEN_REPO, PRODUCER_HASH, SHA, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import derive_architecture
    from xraymyrepo.persistence import persist_snapshot
    from xraymyrepo.persistence import queries as q

API = "/api/v1"
DB = "backend/app/db.py"
PROJECT = "group:components:pkg:pypi:acme-backend"


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(
        GOLDEN_REPO, repository=golden.snapshot.repository, commit_sha=golden.snapshot.commit_sha
    )


@pytest.fixture(scope="module")
def stored(golden: SnapshotDocument, analyzed: SnapshotDocument) -> dict[str, int]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        golden_id = persist_snapshot(conn, golden)
        ids = {
            "golden": golden_id,
            "analyzed": persist_snapshot(conn, analyzed),
            "run": derive_architecture(conn, golden_id),
        }
        repository = conn.execute("SELECT id FROM repository").fetchone()[0]
        ids["pending"] = conn.execute(
            "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
            "cim_schema_version, config) VALUES (%s, %s, '9.0.0', %s, '1.0', '{}') RETURNING id",
            (repository, SHA, PRODUCER_HASH),
        ).fetchone()[0]
    return ids


def app_client(cache_size: int = 4) -> TestClient:
    assert DATABASE_URL is not None
    return TestClient(create_app(Settings(database_url=DATABASE_URL, impact_cache_size=cache_size)))


@pytest.fixture(scope="module")
def client(stored: dict[str, int]) -> Iterator[TestClient]:
    with app_client() as test_client:
        yield test_client


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    assert psycopg is not None
    executed: list[str] = []
    original = psycopg.Cursor.execute

    def counting(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(query))
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counting)
    return executed


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code


# ---------------------------------------------------------------------------
# Database-loaded graphs


@pytest.mark.parametrize("name", ["golden", "analyzed"])
def test_loaded_graph_equals_in_memory_graph(
    stored: dict[str, int], golden: SnapshotDocument, analyzed: SnapshotDocument, name: str
) -> None:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        records = q.load_graph_records(conn, stored[name])
    loaded = DependencyGraph.from_parts(
        records.nodes, records.edges, records.roles, records.unresolved_imports
    )
    expected = DependencyGraph.from_document(golden if name == "golden" else analyzed)
    assert loaded.parts() == expected.parts()
    assert impact(loaded, [DB]) == impact(expected, [DB])


# ---------------------------------------------------------------------------
# Impact report


def test_report_matches_the_pure_traversal(
    client: TestClient, stored: dict[str, int], golden: SnapshotDocument
) -> None:
    expected = impact(DependencyGraph.from_document(golden), [DB], ImpactParams(max_depth=4))
    path = f"{API}/snapshots/{stored['golden']}/impact"
    report = ok(client.get(path, params={"node": DB, "depth": 4}))
    assert report["seeds"] == [{"key": DB, "kind": "file", "name": "db.py"}]
    assert [
        (i["distance"], i["category"], i["confidence"], i["node"]["key"]) for i in report["items"]
    ] == [(i.distance, i.category, i.confidence.value, i.key) for i in expected.items]
    assert [i["via"] for i in report["items"]] == [
        [r.model_dump(mode="json") for r in i.via] for i in expected.items
    ]
    assert report["summary"] == {
        "max_depth": 4,
        "affected_files": 6,
        "affected_tests": 1,
        "affected_endpoints": 1,
        "by_distance": {"0": 1, "1": 1, "2": 1, "3": 3, "4": 1},
        "by_confidence": {"high": 6, "medium": 1},
        "truncated": False,
        "beyond_depth": False,
        "blind_spot_files": 2,
    }
    # Pages of one item reproduce the full list; the summary is the same on every page.
    items, cursor = [], None
    while True:
        params: dict[str, Any] = {"node": DB, "depth": 4, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = ok(client.get(path, params=params))
        assert page["summary"] == report["summary"]
        items += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert items == report["items"]
    tests = ok(client.get(path, params={"node": DB, "category": "test"}))["items"]
    assert [i["node"]["key"] for i in tests] == ["backend/tests/test_users.py"]


def test_several_seeds(client: TestClient, stored: dict[str, int]) -> None:
    report = ok(
        client.get(
            f"{API}/snapshots/{stored['golden']}/impact",
            params={"node": [DB, "ext:pypi:fastapi"], "depth": 1},
        )
    )
    assert [s["key"] for s in report["seeds"]] == [DB, "ext:pypi:fastapi"]
    assert {i["node"]["key"] for i in report["items"] if i["distance"] == 1} == {
        "backend/app/users/models.py",
        "backend/app/main.py",
        "backend/app/users/routes.py",
        # routes.py#get_user handles it: same distance as the handler
        "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend",
    }


def test_impact_errors(client: TestClient, stored: dict[str, int]) -> None:
    path = f"{API}/snapshots/{stored['golden']}/impact"
    problem(client.get(path), 422, "validation_error")
    problem(client.get(path, params={"node": "a//b"}), 422, "invalid_key")
    problem(client.get(path, params={"node": "backend/nope.py"}), 404, "node_not_found")
    problem(client.get(path, params={"node": "/"}), 422, "invalid_parameters")
    problem(
        client.get(path, params={"node": [f"x{i}.py" for i in range(51)]}),
        422,
        "invalid_parameters",
    )
    problem(client.get(path, params={"node": DB, "depth": 11}), 422, "validation_error")
    problem(client.get(path, params={"node": DB, "cursor": "x"}), 400, "invalid_cursor")
    problem(
        client.get(f"{API}/snapshots/{stored['pending']}/impact", params={"node": DB}),
        409,
        "snapshot_not_complete",
    )


def test_blind_spots(client: TestClient, stored: dict[str, int]) -> None:
    spots = ok(client.get(f"{API}/snapshots/{stored['golden']}/impact/blind-spots"))["items"]
    assert spots == [
        {
            "node": {"key": "backend/app/legacy.py", "kind": "file", "name": "legacy.py"},
            "extraction_status": "failed",
            "unresolved_imports": {},
        },
        {
            "node": {"key": "backend/app/plugins.py", "kind": "file", "name": "plugins.py"},
            "extraction_status": "full",
            "unresolved_imports": {"dynamic": 1, "not_found": 1},
        },
    ]
    paged = ok(
        client.get(f"{API}/snapshots/{stored['golden']}/impact/blind-spots", params={"limit": 1})
    )
    assert len(paged["items"]) == 1 and paged["next_cursor"]


# ---------------------------------------------------------------------------
# Affected groups of an architecture run


def test_impact_per_group(client: TestClient, stored: dict[str, int]) -> None:
    path = f"{API}/derivations/{stored['run']}/impact/groups"
    top = ok(client.get(path, params={"node": DB, "lens": "components", "depth": 4}))
    # Every affected file is a Python module of the acme-backend project.
    assert (top["affected_files"], top["direct_files"], top["unassigned_files"]) == (6, 0, 0)
    assert top["items"] == [
        {"key": PROJECT, "label": "acme-backend", "affected_files": 6, "min_distance": 0}
    ]
    inside = ok(
        client.get(path, params={"node": DB, "lens": "components", "parent": PROJECT, "depth": 4})
    )
    assert [(g["label"], g["affected_files"], g["min_distance"]) for g in inside["items"]] == [
        ("app", 2, 0),
        ("app.users", 3, 1),
        ("tests", 1, 3),
    ]
    directories = ok(
        client.get(
            path, params={"node": DB, "lens": "directory", "parent": "group:directory:backend/app"}
        )
    )
    # db.py sits directly in backend/app; the users files are in its child group.
    assert directories["direct_files"] == 1
    assert [(g["key"], g["affected_files"]) for g in directories["items"]] == [
        ("group:directory:backend/app/users", 3)
    ]
    assert directories["unassigned_files"] == 1  # backend/tests/test_users.py


def test_impact_per_group_errors(client: TestClient, stored: dict[str, int]) -> None:
    path = f"{API}/derivations/{stored['run']}/impact/groups"
    problem(client.get(path, params={"node": DB, "lens": "nope"}), 404, "lens_not_found")
    problem(
        client.get(path, params={"node": DB, "lens": "components", "parent": f"{PROJECT}#x"}),
        404,
        "group_not_found",
    )
    problem(
        client.get(path, params={"node": DB, "lens": "components", "parent": "group:directory:/"}),
        404,
        "group_not_found",
    )
    problem(client.get(path, params={"node": DB}), 422, "validation_error")
    problem(
        client.get(
            f"{API}/derivations/999999/impact/groups", params={"node": DB, "lens": "components"}
        ),
        404,
        "derivation_not_found",
    )


# ---------------------------------------------------------------------------
# Cache behaviour and query budgets


GRAPH_LOAD = 4  # nodes, edges, roles, unresolved imports


def test_cold_then_warm_cache(stored: dict[str, int], statements: list[str]) -> None:
    with app_client() as fresh:
        cache = fresh.app.state.graph_cache  # type: ignore[attr-defined]
        url = f"{API}/snapshots/{stored['golden']}/impact"
        statements.clear()
        cold = ok(fresh.get(url, params={"node": DB}))
        assert len(statements) == 1 + GRAPH_LOAD, statements  # snapshot check + load
        statements.clear()
        warm = ok(fresh.get(url, params={"node": DB}))
        assert len(statements) == 1, statements  # snapshot check only
        assert warm == cold and (cache.misses, cache.hits) == (1, 1)


def test_cache_is_bounded_and_can_be_disabled(
    stored: dict[str, int], statements: list[str]
) -> None:
    golden = f"{API}/snapshots/{stored['golden']}/impact"
    analyzed = f"{API}/snapshots/{stored['analyzed']}/impact"
    with app_client(cache_size=1) as one:
        cache = one.app.state.graph_cache  # type: ignore[attr-defined]
        for url in (golden, analyzed, golden):
            ok(one.get(url, params={"node": DB}))
        assert (cache.misses, cache.hits, len(cache)) == (3, 0, 1)  # each evicts the other
        ok(one.get(golden, params={"node": DB}))
        assert cache.hits == 1
    with app_client(cache_size=0) as none:
        for _ in range(2):
            statements.clear()
            ok(none.get(golden, params={"node": DB}))
            assert len(statements) == 1 + GRAPH_LOAD


BUDGET = [
    ("/snapshots/{golden}/impact", {"node": DB}, 1),
    ("/snapshots/{golden}/impact/blind-spots", {}, 1),
    ("/derivations/{run}/impact/groups", {"node": DB, "lens": "components"}, 4),
    ("/derivations/{run}/impact/groups", {"node": DB, "lens": "components", "parent": PROJECT}, 4),
]


@pytest.mark.parametrize(("path", "params", "budget"), BUDGET)
def test_query_budget_with_a_warm_cache(
    client: TestClient,
    stored: dict[str, int],
    statements: list[str],
    path: str,
    params: dict[str, Any],
    budget: int,
) -> None:
    url = API + path.format(**stored)
    ok(client.get(url, params=params))  # warm the cache
    for limit in (1, 1000):
        statements.clear()
        ok(client.get(url, params={**params, "limit": limit}))
        assert len(statements) == budget, statements
