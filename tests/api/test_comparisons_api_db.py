"""Comparison API (``/api/v1/comparisons/...``) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database
(the schema is dropped and recreated). Comparisons of the comparison fixture
(``fixtures/comparison/v1``) are stored once through
``orchestration.compare_snapshots``; every response is compared with the in-memory
``SnapshotComparison`` that ``compare`` returns for the same documents.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.analyzer import analyze
from xraymyrepo.api import Settings, create_app, cursors
from xraymyrepo.cim import (
    EdgeChange,
    NodeChange,
    RepositoryRef,
    SnapshotComparison,
    SnapshotDocument,
)
from xraymyrepo.insights import comparison as algorithm
from xraymyrepo.insights.comparison import ComparisonParams, compare

from ..conftest import ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import compare_snapshots
    from xraymyrepo.persistence import persist_snapshot

API = "/api/v1"
FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")


def with_identity(doc: SnapshotDocument, **identity: Any) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    return SnapshotDocument.model_validate(data)


@pytest.fixture(scope="module")
def docs() -> dict[str, SnapshotDocument]:
    before = analyze(FIXTURE / "before", repository=REPO, commit_sha="1" * 40)
    after = analyze(FIXTURE / "after", repository=REPO, commit_sha="2" * 40)
    return {
        "before": before,
        "after": after,
        "other_version": with_identity(after, commit_sha="3" * 40, extractor_version="0.3.0"),
        "unrelated": with_identity(after, commit_sha="5" * 40),
    }


@pytest.fixture(scope="module")
def stored(docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        ids = {name: persist_snapshot(conn, doc) for name, doc in docs.items()}
        ids["forward"] = compare_snapshots(conn, ids["before"], ids["after"])
        ids["backward"] = compare_snapshots(conn, ids["after"], ids["before"])
        ids["incomparable"] = compare_snapshots(conn, ids["before"], ids["other_version"])
        for name, params_hash in (("pending", "sha256:" + "e" * 64),
                                  ("failed", "sha256:" + "f" * 64)):  # fmt: skip
            ids[name] = conn.execute(
                "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
                "algorithm_version, params_hash, params) VALUES (%s, %s, %s, '9.9.9', %s, '{}') "
                "RETURNING id",
                (ids["before"], ids["after"], algorithm.ALGORITHM, params_hash),
            ).fetchone()[0]
        conn.execute(
            "UPDATE snapshot_comparison SET status = 'failed', failure_reason = 'boom', "
            "finished_at = now() WHERE id = %s",
            (ids["failed"],),
        )
    return ids


@pytest.fixture(scope="module")
def client(stored: dict[str, int]) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def expected(docs: dict[str, SnapshotDocument]) -> dict[str, SnapshotComparison]:
    return {
        "forward": compare(docs["before"], docs["after"]),
        "backward": compare(docs["after"], docs["before"]),
        "incomparable": compare(docs["before"], docs["other_version"]),
    }


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
        params = {**params, "cursor": page["next_cursor"]}


def node_view(n: NodeChange) -> dict[str, Any]:
    return {"key": n.key, **n.model_dump(mode="json")}


def edge_view(e: EdgeChange) -> dict[str, Any]:
    return e.model_dump(mode="json")


def url(stored: dict[str, int], name: str, tail: str = "") -> str:
    return f"{API}/comparisons/{stored[name]}{tail}"


# ---------------------------------------------------------------------------
# A comparison and its summary


@pytest.mark.parametrize("name", ["forward", "backward", "incomparable"])
def test_detail(client: TestClient, stored: dict[str, int],
                expected: dict[str, SnapshotComparison], name: str) -> None:  # fmt: skip
    c = expected[name]
    body = ok(client.get(url(stored, name)))
    run = c.run
    assert (body["id"], body["algorithm"], body["algorithm_version"], body["params_hash"]) == (
        stored[name], run.algorithm, run.algorithm_version, run.params_hash)  # fmt: skip
    assert body["incomparable"] == [r.value for r in run.incomparable]
    assert body["status"] == "complete" and body["failure_reason"] is None
    assert body["finished_at"] is not None
    assert body["params"] == ComparisonParams().as_json()
    assert body["counts"] == c.summary().model_dump(mode="json")
    for side, identity in (("from_snapshot", run.from_snapshot), ("to_snapshot", run.to_snapshot)):
        assert body[side]["commit_sha"] == identity.commit_sha
        assert body[side]["extractor_version"] == identity.extractor_version
        assert body[side]["id"] == body[f"{side}_id"]


def test_summary_counts_every_kind(client: TestClient, stored: dict[str, int],
                                   expected: dict[str, SnapshotComparison]) -> None:  # fmt: skip
    body = ok(client.get(url(stored, "forward", "/summary")))
    assert body == expected["forward"].summary().model_dump(mode="json")
    assert body["edges"]["unchanged"] == expected["forward"].unchanged_edges == 1
    assert set(body["nodes"]) == {"added", "removed", "modified", "moved", "unchanged",
                                  "undetermined"}  # fmt: skip


def test_comparisons_of_a_snapshot(client: TestClient, stored: dict[str, int]) -> None:
    """Either side, newest first, any status; paged."""
    listed = all_pages(client, f"{API}/snapshots/{stored['after']}/comparisons", limit=2)
    assert [c["id"] for c in listed] == sorted(
        [stored[n] for n in ("forward", "backward", "pending", "failed")], reverse=True
    )
    by_id = {c["id"]: c for c in listed}
    assert by_id[stored["failed"]]["status"] == "failed"
    assert by_id[stored["failed"]]["failure_reason"] == "boom"
    assert by_id[stored["pending"]]["status"] == "pending"
    assert ok(client.get(f"{API}/snapshots/{stored['unrelated']}/comparisons"))["items"] == []


# ---------------------------------------------------------------------------
# Node and edge changes


@pytest.mark.parametrize("name", ["forward", "backward", "incomparable"])
def test_node_changes_are_every_node_in_key_order(
    client: TestClient, stored: dict[str, int], expected: dict[str, SnapshotComparison], name: str
) -> None:
    items = all_pages(client, url(stored, name, "/node-changes"), limit=4)
    assert items == [node_view(n) for n in expected[name].nodes]
    assert [i["key"] for i in items] == sorted(i["key"] for i in items)


@pytest.mark.parametrize("name", ["forward", "backward", "incomparable"])
def test_edge_changes_in_canonical_order(
    client: TestClient, stored: dict[str, int], expected: dict[str, SnapshotComparison], name: str
) -> None:
    items = all_pages(client, url(stored, name, "/edge-changes"), limit=1)
    assert items == [edge_view(e) for e in expected[name].edges]
    assert all(i["change"] != "unchanged" for i in items)


@pytest.mark.parametrize(
    "params",
    [
        {"change": "undetermined"},
        {"change": ["added", "moved"]},
        {"kind": "function"},
        {"kind": ["file", "directory"], "change": "removed"},
        {"change": "moved", "kind": "endpoint"},  # matches nothing: an empty page
    ],
)
def test_node_change_filters(client: TestClient, stored: dict[str, int],
                             expected: dict[str, SnapshotComparison],
                             params: dict[str, Any]) -> None:  # fmt: skip
    def wanted(values: Any) -> set[str] | None:
        return None if values is None else set([values] if isinstance(values, str) else values)

    changes, kinds = wanted(params.get("change")), wanted(params.get("kind"))
    items = all_pages(client, url(stored, "forward", "/node-changes"), **params, limit=2)
    assert items == [node_view(n) for n in expected["forward"].nodes
                     if (changes is None or n.change.value in changes)
                     and (kinds is None or n.kind.value in kinds)]  # fmt: skip


def test_undetermined_rows_keep_their_sides(client: TestClient, stored: dict[str, int]) -> None:
    one_sided = ok(client.get(url(stored, "forward", "/node-changes"), params={
        "change": "undetermined"}))["items"]  # fmt: skip
    assert one_sided == [{"key": "shop/report.py#summarize", "change": "undetermined",
                          "kind": "function", "from_key": "shop/report.py#summarize",
                          "to_key": None, "match_method": None, "confidence": "low",
                          "move_ambiguous": False}]  # fmt: skip
    two_sided = ok(client.get(url(stored, "incomparable", "/node-changes"), params={
        "change": "undetermined", "kind": "type"}))["items"]  # fmt: skip
    assert two_sided and all(
        i["from_key"] == i["to_key"] and i["match_method"] == "same_key" for i in two_sided
    )
    edge = ok(client.get(url(stored, "forward", "/edge-changes"), params={
        "change": "undetermined"}))["items"]  # fmt: skip
    assert len(edge) == 1 and edge[0]["to_ref"] is None and edge[0]["confidence"] == "low"


def test_edge_change_filters(client: TestClient, stored: dict[str, int],
                             expected: dict[str, SnapshotComparison]) -> None:  # fmt: skip
    edges = expected["forward"].edges
    modified = ok(client.get(url(stored, "forward", "/edge-changes"), params={
        "change": ["modified", "added"]}))["items"]  # fmt: skip
    assert modified == [edge_view(e) for e in edges if e.change.value in ("modified", "added")]
    imports = ok(client.get(url(stored, "forward", "/edge-changes"), params={"kind": "IMPORTS"}))
    assert imports["items"] == [edge_view(e) for e in edges]  # the fixture's are all IMPORTS
    assert ok(client.get(url(stored, "forward", "/edge-changes"),
                         params={"kind": "CALLS"}))["items"] == []  # fmt: skip


def test_moved_rows_carry_both_keys(client: TestClient, stored: dict[str, int]) -> None:
    moved = ok(client.get(url(stored, "forward", "/node-changes"), params={"change": "moved"}))
    assert [(i["from_key"], i["to_key"], i["match_method"], i["confidence"])
            for i in moved["items"]] == [
        ("shop/old/shipping.py", "shop/new/shipping.py", "same_content_hash", "medium"),
        ("shop/old/shipping.py#shipping_cost", "shop/new/shipping.py#shipping_cost",
         "same_content_hash", "medium"),
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Errors


@pytest.mark.parametrize("tail", ["", "/summary", "/node-changes", "/edge-changes"])
def test_missing_and_incomplete_comparisons(client: TestClient, stored: dict[str, int],
                                            tail: str) -> None:  # fmt: skip
    problem(client.get(f"{API}/comparisons/999999{tail}"), 404, "comparison_not_found")
    body = problem(client.get(url(stored, "pending", tail)), 409, "comparison_not_complete")
    assert "pending" in body["detail"]
    body = problem(client.get(url(stored, "failed", tail)), 409, "comparison_not_complete")
    assert "failed" in body["detail"]


@pytest.mark.parametrize("comparison_id", ["0", "-1", "abc", "1.5"])
def test_malformed_comparison_ids(client: TestClient, comparison_id: str) -> None:
    for tail in ("", "/summary", "/node-changes", "/edge-changes"):
        problem(client.get(f"{API}/comparisons/{comparison_id}{tail}"), 422, "validation_error")


def test_snapshot_listing_errors(client: TestClient, stored: dict[str, int]) -> None:
    problem(client.get(f"{API}/snapshots/999999/comparisons"), 404, "snapshot_not_found")
    problem(client.get(f"{API}/snapshots/0/comparisons"), 422, "validation_error")
    listing = f"{API}/snapshots/{stored['after']}/comparisons"
    for tampered in (2**63, -1):  # outside a bigint id: rejected, never a 500
        problem(client.get(listing, params={"cursor": cursors.encode(tampered)}), 400,
                "invalid_cursor")  # fmt: skip


@pytest.mark.parametrize(
    ("tail", "params"),
    [
        ("/node-changes", {"limit": 0}),
        ("/node-changes", {"limit": 1001}),
        ("/node-changes", {"change": "renamed"}),
        ("/node-changes", {"kind": "group"}),  # a derivation-tier kind, never a snapshot node
        ("/edge-changes", {"kind": "USES"}),
        ("/edge-changes", {"change": "moved"}),  # nodes move; edges do not
        ("/edge-changes", {"limit": "x"}),
    ],
)
def test_invalid_parameters(client: TestClient, stored: dict[str, int], tail: str,
                            params: dict[str, Any]) -> None:  # fmt: skip
    body = problem(client.get(url(stored, "forward", tail), params=params), 422,
                   "validation_error")  # fmt: skip
    assert body["errors"]


def test_unchanged_edges_are_not_listed(client: TestClient, stored: dict[str, int]) -> None:
    for change in ("unchanged", ["added", "unchanged"]):
        response = client.get(url(stored, "forward", "/edge-changes"), params={"change": change})
        body = problem(response, 422, "validation_error")
        assert "see the comparison's summary" in body["errors"][0]["msg"]


@pytest.mark.parametrize(
    ("tail", "cursor"),
    [
        ("/node-changes", "%%%"),
        ("/node-changes", cursors.encode(3)),  # an edge-change cursor
        ("/edge-changes", cursors.encode("shop/a.py")),  # a node-change cursor
        ("/edge-changes", cursors.encode([1, 2])),
        # Tampered values outside what the SQL compares with: rejected, never a 500.
        ("/edge-changes", cursors.encode(2**63)),  # beyond a bigint id
        ("/edge-changes", cursors.encode(-1)),
        ("/node-changes", cursors.encode("a\x00b.py")),  # PostgreSQL text holds no NUL
    ],
)
def test_invalid_cursors(client: TestClient, stored: dict[str, int], tail: str,
                         cursor: str) -> None:  # fmt: skip
    problem(client.get(url(stored, "forward", tail), params={"cursor": cursor}), 400,
            "invalid_cursor")  # fmt: skip


def test_the_api_is_read_only(client: TestClient, stored: dict[str, int]) -> None:
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(url(stored, "forward"))
        problem(response, 405, "method_not_allowed")
    problem(client.post(f"{API}/snapshots/{stored['before']}/comparisons"), 405,
            "method_not_allowed")  # fmt: skip


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


@pytest.mark.parametrize(
    ("path", "params", "budget", "paged"),
    [
        ("/snapshots/{after}/comparisons", {}, 2, True),
        ("{c}", {}, 5, False),
        ("{c}/summary", {}, 2, False),
        ("{c}/node-changes", {}, 2, True),
        ("{c}/node-changes", {"change": "added", "kind": "file"}, 2, True),
        ("{c}/edge-changes", {}, 2, True),
    ],
)
def test_query_budget(client: TestClient, stored: dict[str, int], statements: list[str],
                      path: str, params: dict[str, Any], budget: int,
                      paged: bool) -> None:  # fmt: skip
    """A fixed number of statements per request, whatever the page size (no N+1)."""
    target = API + path.format(after=stored["after"], c=f"/comparisons/{stored['forward']}")
    for limit in (1, 1000) if paged else (None,):
        statements.clear()
        ok(client.get(target, params={**params, "limit": limit} if limit else params))
        assert len(statements) == budget, statements
