"""Architecture intelligence API (``/api/v1/derivations/...``) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` is set. Runs are created once through
``orchestration.derive_architecture``; every response is compared with the
in-memory ``Derivation`` that ``derive`` returns for the same snapshot.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.api import Settings, create_app, cursors
from xraymyrepo.architecture import ArchitectureParams, derive
from xraymyrepo.cim import Derivation, SnapshotDocument

from ..conftest import PRODUCER_HASH, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import derive_architecture
    from xraymyrepo.persistence import persist_snapshot

API = "/api/v1"
PROJECT = "group:components:pkg:pypi:acme-backend"
USERS = f"{PROJECT}#app.users"
APP = f"{PROJECT}#app"


@pytest.fixture(scope="module")
def stored(golden: SnapshotDocument) -> dict[str, int]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        snapshot = persist_snapshot(conn, golden)
        run = derive_architecture(conn, snapshot)
        pending = conn.execute(
            "INSERT INTO derivation_run (snapshot_id, algorithm, algorithm_version, params_hash, "
            "params) VALUES (%s, 'architecture', '9.9.9', %s, '{}') RETURNING id",
            (snapshot, PRODUCER_HASH),
        ).fetchone()[0]
    return {"snapshot": snapshot, "run": run, "pending": pending}


@pytest.fixture(scope="module")
def client(stored: dict[str, int]) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def run(stored: dict[str, int]) -> str:
    return f"{API}/derivations/{stored['run']}"


@pytest.fixture(scope="module")
def expected(golden: SnapshotDocument) -> Derivation:
    return derive(golden)


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def problem(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert (body["status"], body["code"]) == (status, code)
    return body


def all_pages(client: TestClient, path: str, **params: Any) -> list[Any]:
    items: list[Any] = []
    while True:
        page = ok(client.get(path, params=params))
        items += page["items"]
        if page["next_cursor"] is None:
            return items
        params["cursor"] = page["next_cursor"]


def subtree(d: Derivation, key: str) -> set[str]:
    found, stack = {key}, [key]
    while stack:
        current = stack.pop()
        for g in d.groups:
            if g.parent_key == current:
                found.add(g.key)
                stack.append(g.key)
    return found


# ---------------------------------------------------------------------------
# Runs


def test_runs_of_a_snapshot(client: TestClient, stored: dict[str, int]) -> None:
    runs = all_pages(client, f"{API}/snapshots/{stored['snapshot']}/derivations", limit=1)
    assert [r["id"] for r in runs] == [stored["pending"], stored["run"]]  # newest first
    assert [r["status"] for r in runs] == ["pending", "complete"]
    problem(client.get(f"{API}/snapshots/999999/derivations"), 404, "snapshot_not_found")


def test_run_detail(client: TestClient, run: str, expected: Derivation) -> None:
    detail = ok(client.get(run))
    identity = (detail["algorithm"], detail["algorithm_version"], detail["params_hash"])
    run_ = expected.run
    assert identity == (run_.algorithm, run_.algorithm_version, run_.params_hash)
    assert detail["params"] == ArchitectureParams().as_json()
    lenses = {lens["name"]: lens for lens in detail["lenses"]}
    assert lenses["components"] == {"name": "components", "kind": "inferred", "group_count": 5,
                                    "member_count": 12}  # fmt: skip
    assert lenses["directory"]["kind"] == "directory"
    assert detail["layer_coverage"] == {
        "considered": 9, "classified": 6, "unclassified": 3,
        "by_layer": {"application": 1, "infrastructure": 2, "interface": 3},
        "by_confidence": {"high": 5, "medium": 1},
    }  # fmt: skip
    assert detail["finding_counts"] == {"arch.group.cycle": 1}


def test_run_gate(client: TestClient, stored: dict[str, int]) -> None:
    problem(client.get(f"{API}/derivations/999999"), 404, "derivation_not_found")
    for path in ("", "/groups/children?lens=components", "/layers", "/findings"):
        problem(client.get(f"{API}/derivations/{stored['pending']}{path}"), 409,
                "derivation_not_complete")  # fmt: skip
    problem(client.get(f"{API}/derivations/0"), 422, "validation_error")


# ---------------------------------------------------------------------------
# Groups


def test_group_tree_through_the_api(client: TestClient, run: str, expected: Derivation) -> None:
    """Expanding every group, two per page, reproduces both lenses' trees."""
    considered = {m.node_key for m in expected.metrics if m.metric == "layer_considered"}
    layers = {x.node_key: x.layer for x in expected.layers}
    by_key = {g.key: g for g in expected.groups}
    for lens in ("components", "directory"):
        seen: list[str] = []
        frontier: list[str | None] = [None]
        while frontier:
            parent = frontier.pop()
            params: dict[str, Any] = {"lens": lens, "limit": 2}
            if parent is not None:
                params["parent"] = parent
            children = all_pages(client, f"{run}/groups/children", **params)
            assert [c["key"] for c in children] == sorted(
                g.key for g in expected.groups if g.lens == lens and g.parent_key == parent
            )
            for child in children:
                group = by_key[child["key"]]
                assert (child["label"], child["basis"], child["confidence"]) == (
                    group.label, group.basis, group.confidence)  # fmt: skip
                assert child["child_group_count"] == sum(g.parent_key == group.key
                                                         for g in expected.groups)  # fmt: skip
                assert child["member_count"] == sum(m.group_key == group.key
                                                    for m in expected.members)  # fmt: skip
                assert child["metrics"] == {m.metric: m.value for m in expected.metrics
                                            if m.node_key == group.key}  # fmt: skip
                files = {m.node_key for m in expected.members
                         if m.group_key in subtree(expected, group.key)} & considered  # fmt: skip
                assert child["layer_mix"] == {
                    "by_layer": dict(sorted(Counter(layers[f] for f in files if f in layers)
                                            .items())),
                    "unclassified": sum(f not in layers for f in files),
                }  # fmt: skip
            seen += [c["key"] for c in children]
            frontier += [c["key"] for c in children if c["child_group_count"]]
        assert sorted(seen) == sorted(g.key for g in expected.groups if g.lens == lens)


def test_group_tree_errors(client: TestClient, run: str) -> None:
    path = f"{run}/groups/children"
    problem(client.get(path, params={"lens": "package"}), 404, "lens_not_found")
    problem(client.get(path), 422, "validation_error")
    problem(client.get(path, params={"lens": "components", "parent": "not-a-group"}), 422,
            "invalid_key")  # fmt: skip
    problem(client.get(path, params={"lens": "components", "parent": f"{PROJECT}#nope"}), 404,
            "group_not_found")  # fmt: skip
    # A group of another lens is not a group of this one.
    problem(client.get(path, params={"lens": "components", "parent": "group:directory:/"}),
            404, "group_not_found")  # fmt: skip
    problem(client.get(path, params={"lens": "components", "cursor": "x"}), 400,
            "invalid_cursor")  # fmt: skip


def test_group_detail(client: TestClient, run: str, expected: Derivation) -> None:
    for group in expected.groups:
        detail = ok(client.get(f"{run}/groups/by-key", params={"key": group.key}))
        dumped = group.model_dump(mode="json")
        assert detail["provenance"] == dumped["provenance"]
        assert detail["evidence"] == dumped["evidence"]
        assert detail["parent_key"] == group.parent_key
    users = ok(client.get(f"{run}/groups/by-key", params={"key": USERS}))
    assert users["ancestors"] == [{"key": PROJECT, "label": "acme-backend"}]
    assert users["evidence"][0]["node_keys"] == ["backend/app/users",
                                                 "backend/app/users/__init__.py"]  # fmt: skip
    assert users["layer_mix"] == {"by_layer": {"application": 1, "infrastructure": 1,
                                               "interface": 2}, "unclassified": 1}  # fmt: skip
    problem(client.get(f"{run}/groups/by-key", params={"key": f"{PROJECT}#x"}), 404,
            "group_not_found")  # fmt: skip
    problem(client.get(f"{run}/groups/by-key", params={"key": "app.users"}), 422, "invalid_key")


def test_group_members(client: TestClient, run: str, expected: Derivation,
                       golden: SnapshotDocument) -> None:  # fmt: skip
    members = all_pages(client, f"{run}/groups/members", group=USERS, limit=2)
    assert [m["key"] for m in members] == sorted(
        m.node_key for m in expected.members if m.group_key == USERS)  # fmt: skip
    for member in members:  # snapshot node summaries of the member files
        assert member["kind"] == "file"
        assert member["child_count"] == len(golden.children(member["key"]))
    assert ok(client.get(f"{run}/groups/members", params={"group": PROJECT}))["items"] == []
    problem(client.get(f"{run}/groups/members", params={"group": f"{PROJECT}#x"}), 404,
            "group_not_found")  # fmt: skip


# ---------------------------------------------------------------------------
# Group edges


def test_zoom_levels_return_every_group_edge_once(client: TestClient, run: str,
                                                  expected: Derivation) -> None:  # fmt: skip
    found: list[Any] = []
    for lens in ("components", "directory"):
        found += all_pages(client, f"{run}/group-edges", lens=lens, limit=2)
    for group in expected.groups:
        found += all_pages(client, f"{run}/group-edges", within=group.key, limit=2)
    key = lambda e: (e["source_group_key"], e["edge_kind"], e["target_group_key"])  # noqa: E731
    assert sorted(found, key=key) == sorted(
        (e.model_dump(mode="json") for e in expected.group_edges), key=key)  # fmt: skip


def test_edges_of_one_group(client: TestClient, run: str, expected: Derivation) -> None:
    out = ok(client.get(f"{run}/group-edges", params={"group": USERS, "direction": "out"}))
    assert out["items"] == [e.model_dump(mode="json") for e in expected.group_edges
                            if e.source_group_key == USERS]  # fmt: skip
    into = ok(client.get(f"{run}/group-edges", params={"group": USERS, "direction": "in"}))
    assert {e["source_group_key"] for e in into["items"]} == {APP, f"{PROJECT}#tests"}
    cycle = ok(client.get(f"{run}/group-edges", params={"group": APP, "kind": "INHERITS"}))
    assert [(e["source_group_key"], e["target_group_key"]) for e in cycle["items"]] == [
        (USERS, APP)]  # fmt: skip
    assert cycle["items"][0]["basis_counts"] == {"resolved": 1}


def test_group_edge_errors(client: TestClient, run: str) -> None:
    path = f"{run}/group-edges"
    problem(client.get(path), 422, "invalid_parameters")
    problem(client.get(path, params={"lens": "components", "group": USERS}), 422,
            "invalid_parameters")  # fmt: skip
    problem(client.get(path, params={"within": PROJECT, "direction": "out"}), 422,
            "invalid_parameters")  # fmt: skip
    problem(client.get(path, params={"lens": "nope"}), 404, "lens_not_found")
    problem(client.get(path, params={"within": f"{PROJECT}#x"}), 404, "group_not_found")
    problem(client.get(path, params={"group": "bad"}), 422, "invalid_key")
    problem(client.get(path, params={"lens": "components", "kind": "DEPENDS_ON"}), 422,
            "validation_error")  # fmt: skip


# ---------------------------------------------------------------------------
# Layers and findings


def test_layers(client: TestClient, run: str, expected: Derivation) -> None:
    listed = all_pages(client, f"{run}/layers", limit=2)
    assert listed == [x.model_dump(mode="json") for x in expected.layers]
    interface = ok(client.get(f"{run}/layers", params={"layer": "interface"}))["items"]
    assert {x["node_key"] for x in interface} == {
        x.node_key for x in expected.layers if x.layer == "interface"}  # fmt: skip
    assert all(x["basis"] == "inferred" and x["provenance"]["rule"] == "arch.layer.resolve"
               for x in listed)  # fmt: skip


def test_layer_of_one_node(client: TestClient, run: str) -> None:
    def status(key: str) -> dict[str, Any]:
        result: dict[str, Any] = ok(client.get(f"{run}/layers/by-node", params={"node": key}))
        return result

    routes = status("backend/app/users/routes.py")
    assert (routes["status"], routes["assignment"]["layer"]) == ("classified", "interface")
    assert routes["node"] == {"key": "backend/app/users/routes.py", "kind": "file",
                              "name": "routes.py"}  # fmt: skip
    assert status("backend/app/plugins.py") == {
        "node": {"key": "backend/app/plugins.py", "kind": "file", "name": "plugins.py"},
        "status": "unclassified", "assignment": None}  # fmt: skip
    for key in ("backend/tests/test_users.py", "README.md", "backend/app/generated/user_pb2.py"):
        assert status(key)["status"] == "out_of_scope", key
    problem(client.get(f"{run}/layers/by-node", params={"node": "x.py"}), 404, "node_not_found")
    problem(client.get(f"{run}/layers/by-node", params={"node": "a//b"}), 422, "invalid_key")


def test_unclassified(client: TestClient, run: str, expected: Derivation) -> None:
    considered = {m.node_key for m in expected.metrics if m.metric == "layer_considered"}
    listed = all_pages(client, f"{run}/layers/unclassified", limit=1)
    assert [n["key"] for n in listed] == sorted(considered - {x.node_key for x in expected.layers})


def test_findings(client: TestClient, run: str, expected: Derivation) -> None:
    listed = all_pages(client, f"{run}/findings", limit=1)
    assert [{k: v for k, v in f.items() if k != "id"} for f in listed] == [
        f.model_dump(mode="json") for f in expected.findings]  # fmt: skip
    assert listed[0]["subject_keys"] == [APP, USERS]
    assert ok(client.get(f"{run}/findings", params={"rule": "arch.layer.violation"}))[
        "items"] == []  # fmt: skip


# ---------------------------------------------------------------------------
# Query budget


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


BUDGET = [
    ("/snapshots/{snapshot}/derivations", {}, 2, True),
    ("{run}", {}, 6, False),
    ("{run}/groups/children", {"lens": "components"}, 6, True),
    ("{run}/groups/children", {"lens": "directory", "parent": "group:directory:backend"}, 6, True),
    ("{run}/groups/by-key", {"key": USERS}, 6, False),
    ("{run}/groups/members", {"group": USERS}, 5, True),
    ("{run}/group-edges", {"lens": "directory"}, 3, True),
    ("{run}/group-edges", {"within": PROJECT}, 3, True),
    ("{run}/group-edges", {"group": USERS, "direction": "in"}, 3, True),
    ("{run}/layers", {}, 2, True),
    ("{run}/layers/by-node", {"node": "backend/app/users/routes.py"}, 3, False),
    ("{run}/layers/by-node", {"node": "backend/app/plugins.py"}, 4, False),
    ("{run}/layers/unclassified", {}, 2, True),
    ("{run}/findings", {}, 2, True),
]  # fmt: skip


@pytest.mark.parametrize(("path", "params", "budget", "paged"), BUDGET)
def test_query_budget(
    client: TestClient,
    stored: dict[str, int],
    statements: list[str],
    path: str,
    params: dict[str, Any],
    budget: int,
    paged: bool,
) -> None:
    """A fixed number of statements per request, whatever the page size (no N+1)."""
    url = API + path.format(snapshot=stored["snapshot"], run=f"/derivations/{stored['run']}")
    for limit in (1, 1000) if paged else (None,):
        statements.clear()
        ok(client.get(url, params={**params, "limit": limit} if limit else params))
        assert len(statements) == budget, statements


@pytest.mark.parametrize("tampered", [2**63, 10**30, -1])  # outside a bigint id
@pytest.mark.parametrize("path", ["/snapshots/{snapshot}/derivations", "{run}/findings"])
def test_tampered_id_cursors_are_invalid(client: TestClient, stored: dict[str, int], path: str,
                                         tampered: int) -> None:  # fmt: skip
    """Rejected as invalid_cursor, never a 500 from the database."""
    url = API + path.format(snapshot=stored["snapshot"], run=f"/derivations/{stored['run']}")
    problem(client.get(url, params={"cursor": cursors.encode(tampered)}), 400, "invalid_cursor")
