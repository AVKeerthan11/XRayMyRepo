"""Read queries (``xraymyrepo.persistence.queries``) against a real PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` is set (see test_persistence_db.py). The
golden document and the analyzer's output for the golden repository are stored
once; every query result is compared with the same question answered on the
in-memory ``SnapshotDocument``.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Edge,
    EdgeKind,
    EdgeRef,
    NodeKind,
    RepositoryRef,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.cim.nodes import Node

from .conftest import GOLDEN_REPO, MIGRATIONS, reset_database
from .test_persistence_db import minimal_document

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.persistence import persist_snapshot
    from xraymyrepo.persistence import queries as q


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        reset_database(connection)
        yield connection


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                   commit_sha=golden.snapshot.commit_sha)  # fmt: skip


@pytest.fixture(scope="module")
def stored(conn: Any, golden: SnapshotDocument, analyzed: SnapshotDocument) -> dict[str, int]:
    """golden first, then the analyzer's snapshot, then a second (minimal) repository."""
    return {
        "golden": persist_snapshot(conn, golden),
        "analyzed": persist_snapshot(conn, analyzed),
        "minimal": persist_snapshot(conn, minimal_document()),
    }


@pytest.fixture(scope="module")
def sid(stored: dict[str, int]) -> int:
    return stored["golden"]


def paged[T, K](fetch: Callable[[K | None, int], Sequence[T]], sort_key: Callable[[T], K],
          limit: int = 3) -> list[T]:  # fmt: skip
    """Follow keyset pages to the end; each page must be full until the last one."""
    items: list[T] = []
    after: K | None = None
    while True:
        page = fetch(after, limit)
        items += page
        if len(page) < limit:
            return items
        after = sort_key(page[-1])


# ---------------------------------------------------------------------------
# Service, repositories, snapshots


def test_required_migrations_are_the_migration_files() -> None:
    assert tuple(p.stem for p in sorted(MIGRATIONS.glob("*.sql"))) == q.REQUIRED_MIGRATIONS


def test_applied_migrations(conn: Any) -> None:
    assert tuple(q.applied_migrations(conn)) == q.REQUIRED_MIGRATIONS


def test_repositories(conn: Any, stored: dict[str, int], golden: SnapshotDocument) -> None:
    repositories = paged(lambda a, n: q.list_repositories(conn, after=a, limit=n),
                         lambda r: q.repository_sort_key(r.repository), limit=1)  # fmt: skip
    assert [r.repository for r in repositories] == [
        RepositoryRef(host="example.com", owner="Acme", name="Empty"),
        golden.snapshot.repository,
    ]
    fixture = repositories[1]
    assert fixture.snapshot_count == 2
    assert fixture.latest_snapshot is not None
    assert fixture.latest_snapshot.id == stored["analyzed"]  # most recently created


def test_get_repository_ignores_case(conn: Any, stored: dict[str, int]) -> None:
    found = q.get_repository(conn, "example.com", "ACME", "empty")
    assert found is not None
    assert found.repository == RepositoryRef(host="example.com", owner="Acme", name="Empty")
    assert q.get_repository(conn, "example.com", "acme", "missing") is None


def test_snapshots(conn: Any, stored: dict[str, int], golden: SnapshotDocument) -> None:
    repo = golden.snapshot.repository
    listed = paged(lambda a, n: q.list_snapshots(conn, repo, after=a, limit=n),
                   lambda s: s.id, limit=1)  # fmt: skip
    assert [s.id for s in listed] == [stored["analyzed"], stored["golden"]]
    assert listed[1].identity == golden.snapshot
    assert listed[1].status is SnapshotStatus.COMPLETE and listed[1].finished_at is not None
    by_version = q.list_snapshots(conn, repo, extractor_version="1.0.0", limit=10)
    assert [s.id for s in by_version] == [stored["golden"]]
    assert q.list_snapshots(conn, repo, status=SnapshotStatus.PENDING, limit=10) == []


def test_snapshot_header_and_setup(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    record = q.get_snapshot(conn, sid)
    assert record is not None
    assert (record.identity, record.cim_schema_version) == (golden.snapshot, "1.0")
    assert q.get_snapshot(conn, 10**9) is None
    setup = q.get_snapshot_setup(conn, sid)
    canonical = golden.canonical()
    assert (setup.config, setup.producers, setup.coverage) == (
        canonical.config, canonical.producers, canonical.coverage)  # fmt: skip


def test_snapshot_stats(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    stats = q.snapshot_stats(conn, sid)
    assert stats.nodes_by_kind == Counter(n.kind for n in golden.nodes)
    assert stats.edges_by_kind == Counter(e.kind for e in golden.edges)
    assert stats.unresolved_by_reason == Counter(u.reason for u in golden.unresolved_references)
    assert stats.issues_by_severity == Counter(i.severity for i in golden.extraction_issues)


# ---------------------------------------------------------------------------
# Nodes and the tree


def test_every_node_by_key(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    for node in golden.nodes:
        assert q.get_node(conn, sid, node.key) == node
        brief = q.get_node_brief(conn, sid, node.key)
        assert brief == q.NodeBrief(node.key, node.kind, node.name)
    assert q.get_node(conn, sid, "no/such/file.py") is None


def test_children_traversal_reproduces_the_tree(
    conn: Any, sid: int, golden: SnapshotDocument
) -> None:
    """Expanding every node from the root, three children per page, visits exactly the
    nodes of the declaration tree, each under its parent, in child_sort_key order."""
    seen: list[Node] = []
    frontier = ["/"]
    while frontier:
        parent = frontier.pop()

        def fetch(after: q.ChildSortKey | None, limit: int, parent: str = parent) -> list[Node]:
            return q.get_children(conn, sid, parent, after=after, limit=limit)

        children = paged(fetch, q.child_sort_key)
        expected = sorted(golden.children(parent), key=q.child_sort_key)
        assert children == expected, parent
        seen += children
        frontier += [c.key for c in children]
    in_tree = {n.key for n in golden.nodes if n.parent_key is not None}
    assert sorted(n.key for n in seen) == sorted(in_tree)


def test_children_order_and_kind_filter(conn: Any, sid: int) -> None:
    root = [n.key for n in q.get_children(conn, sid, "/", limit=100)]
    assert root == ["backend", "frontend", "scripts", "README.md"]  # directories first
    files = q.get_children(conn, sid, "/", kinds=[NodeKind.FILE], limit=100)
    assert [n.key for n in files] == ["README.md"]
    members = q.get_children(conn, sid, "backend/app/users/service.py#UserService", limit=100)
    assert [n.name for n in members] == ["__init__", "get"]  # source order


def test_count_children(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    keys = [n.key for n in golden.nodes]
    assert q.count_children(conn, sid, keys) == {k: len(golden.children(k)) for k in keys}
    assert q.count_children(conn, sid, ["missing"]) == {}


def test_ancestors(conn: Any, sid: int) -> None:
    path = q.get_ancestors(conn, sid, "backend/app/users/service.py#UserService.get")
    assert [a.key for a in path] == [
        "/", "backend", "backend/app", "backend/app/users", "backend/app/users/service.py",
        "backend/app/users/service.py#UserService",
    ]  # fmt: skip
    assert q.get_ancestors(conn, sid, "/") == []
    assert q.get_ancestors(conn, sid, "ext:pypi:fastapi") == []  # not in the tree


def test_list_nodes(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    kinds = [NodeKind.PACKAGE, NodeKind.EXTERNAL_PACKAGE]
    listed = paged(lambda a, n: q.list_nodes(conn, sid, kinds=kinds, after=a, limit=n),
                   lambda node: node.key)  # fmt: skip
    assert listed == sorted((n for n in golden.nodes if n.kind in kinds), key=lambda n: n.key)
    prefixed = q.list_nodes(conn, sid, prefix="backend/app/users/", limit=100)
    assert [n.key for n in prefixed] == sorted(
        n.key for n in golden.nodes if n.key.startswith("backend/app/users/")
    )
    # LIKE wildcards in the prefix are literal characters.
    assert q.list_nodes(conn, sid, prefix="backend_", limit=10) == []
    assert q.list_nodes(conn, sid, prefix="%", limit=10) == []


def test_tags_and_classifications(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    keys = [n.key for n in golden.nodes]
    expected: dict[str, list[tuple[Any, str]]] = {}
    for c in sorted(golden.classifications, key=lambda c: (c.node_key, c.facet, c.value)):
        expected.setdefault(c.node_key, []).append((c.facet, c.value))
    assert q.get_tags(conn, sid, keys) == expected
    for key in keys:
        assert q.get_classifications(conn, sid, key) == sorted(
            golden.classifications_of(key), key=lambda c: (c.facet, c.value)
        )


# ---------------------------------------------------------------------------
# Edges


def test_every_edge_is_seen_once_from_each_end(
    conn: Any, sid: int, golden: SnapshotDocument
) -> None:
    outgoing: list[Edge] = []
    incoming: list[Edge] = []
    for node in golden.nodes:

        def fetch(after: q.EdgeSortKey | None, limit: int, key: str = node.key) -> list[q.NodeEdge]:
            return q.get_edges(conn, sid, key, after=after, limit=limit)

        items = paged(fetch, q.edge_sort_key)
        assert items == sorted(items, key=q.edge_sort_key)
        for item in items:
            focus = (
                item.edge.source_key if item.direction is q.Direction.OUT else item.edge.target_key
            )
            other = (
                item.edge.target_key if item.direction is q.Direction.OUT else item.edge.source_key
            )
            assert focus == node.key and item.peer.key == other
            (outgoing if item.direction is q.Direction.OUT else incoming).append(item.edge)
    key = lambda e: (e.kind, e.source_key, e.target_key)  # noqa: E731
    assert sorted(outgoing, key=key) == sorted(golden.edges, key=key)
    assert sorted(incoming, key=key) == sorted(golden.edges, key=key)


def test_edge_direction_and_kind_filters(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    target = "backend/app/users/service.py#UserService"
    incoming = q.get_edges(conn, sid, target, direction=q.Direction.IN, limit=100)
    assert {(i.edge.kind, i.peer.key) for i in incoming} == {
        (e.kind, e.source_key) for e in golden.edges if e.target_key == target
    }
    assert all(i.edge.target_key == target for i in incoming)  # never inverted
    tests = q.get_edges(conn, sid, target, direction=q.Direction.IN, kinds=[EdgeKind.TESTS],
                        limit=100)  # fmt: skip
    assert {i.edge.kind for i in tests} <= {EdgeKind.TESTS}
    assert q.get_edges(conn, sid, target, direction=q.Direction.OUT,
                       kinds=[EdgeKind.REQUIRES], limit=100) == []  # fmt: skip


def test_edge_counts(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    for node in golden.nodes:
        expected: dict[EdgeKind, tuple[int, int]] = {}
        for kind in EdgeKind:
            out = sum(e.kind is kind and e.source_key == node.key for e in golden.edges)
            in_ = sum(e.kind is kind and e.target_key == node.key for e in golden.edges)
            if out or in_:
                expected[kind] = (out, in_)
        assert q.edge_counts(conn, sid, node.key) == expected, node.key


def test_every_edge_by_ref(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    for e in golden.edges:
        assert q.get_edge(conn, sid, EdgeRef.of(e.kind, e.source_key, e.target_key)) == e
    some = golden.edges[0]
    assert q.get_edge(conn, sid, EdgeRef.of(some.kind, some.target_key, some.source_key)) is None


# ---------------------------------------------------------------------------
# Search and diagnostics


def test_search_ranks_name_matches_before_paths(conn: Any, sid: int) -> None:
    hits = q.search_nodes(conn, sid, "userservice", limit=100)
    assert [(h.node.key, h.rank, h.matched) for h in hits[:1]] == [
        ("backend/app/users/service.py#UserService", 0, "name")
    ]
    assert {h.matched for h in hits[1:]} == {"key"}  # its members, matched by key
    paths = q.search_nodes(conn, sid, "users/service", limit=100)
    assert paths and all(h.rank == 3 and "users/service" in h.node.key for h in paths)


def test_search_paging_and_filters(conn: Any, sid: int) -> None:
    every = q.search_nodes(conn, sid, "user", limit=1000)
    assert len(every) > 5
    pages = paged(lambda a, n: q.search_nodes(conn, sid, "user", after=a, limit=n),
                  q.search_sort_key)  # fmt: skip
    assert pages == every
    functions = q.search_nodes(conn, sid, "user", kinds=[NodeKind.FUNCTION], limit=100)
    assert functions and all(h.node.kind is NodeKind.FUNCTION for h in functions)
    # LIKE wildcards in the query are literal characters.
    assert q.search_nodes(conn, sid, "%", limit=10) == []
    underscores = q.search_nodes(conn, sid, "_", limit=1000)
    assert underscores and all("_" in h.node.key for h in underscores)
    assert len(underscores) < len(q.search_nodes(conn, sid, "", limit=1000))


def test_diagnostics(conn: Any, sid: int, golden: SnapshotDocument) -> None:
    for node in golden.nodes:
        found = q.get_diagnostics(conn, sid, node.key)
        expected_refs = [u for u in golden.canonical().unresolved_references
                         if node.key in (u.source_key, u.file_key)]  # fmt: skip
        expected_issues = [i for i in golden.extraction_issues if i.file_key == node.key]
        assert list(found.unresolved_references) == expected_refs, node.key
        assert list(found.extraction_issues) == expected_issues, node.key
        counts = (len(expected_refs), len(expected_issues))
        assert q.diagnostic_counts(conn, sid, node.key) == counts, node.key
    assert q.get_diagnostics(conn, sid, "backend/app/plugins.py").unresolved_references
