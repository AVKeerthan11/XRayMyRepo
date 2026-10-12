"""Change impact (no database): golden expectations, traversal rules, bounds, witness
paths, blind spots, determinism and performance."""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Basis,
    Confidence,
    EdgeKind,
    EdgeRef,
    NodeKind,
    RepositoryRef,
    SnapshotDocument,
    UnresolvedReason,
)
from xraymyrepo.graph import DependencyGraph, GraphEdge, GraphNode
from xraymyrepo.insights.impact import ImpactError, ImpactParams, ImpactResult, impact

from ..conftest import GOLDEN_REPO

DB = "backend/app/db.py"
SERVICE = "backend/app/users/service.py"
USER_SERVICE = f"{SERVICE}#UserService"
ENDPOINT = "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend"


@pytest.fixture(scope="module")
def graph(golden: SnapshotDocument) -> DependencyGraph:
    return DependencyGraph.from_document(golden)


@pytest.fixture(scope="module")
def analyzed_graph(golden: SnapshotDocument) -> DependencyGraph:
    doc = analyze(
        GOLDEN_REPO, repository=golden.snapshot.repository, commit_sha=golden.snapshot.commit_sha
    )
    return DependencyGraph.from_document(doc)


def table(result: ImpactResult) -> list[tuple[int, str, str, str]]:
    return [(i.distance, i.category, i.confidence.value, i.key) for i in result.items]


# ---------------------------------------------------------------------------
# Golden


def test_golden_impact_of_db(graph: DependencyGraph) -> None:
    result = impact(graph, [DB])
    assert table(result) == [
        (0, "file", "high", DB),
        (1, "file", "high", "backend/app/users/models.py"),
        (2, "file", "high", SERVICE),
        (3, "file", "high", "backend/app/users/routes.py"),
        (3, "test", "high", "backend/tests/test_users.py"),
        # An endpoint has its handler's distance; HANDLES is heuristic, hence medium.
        (3, "endpoint", "medium", ENDPOINT),
    ]
    assert result.beyond_depth and not result.truncated  # main.py is 4 hops away
    deeper = impact(graph, [DB], ImpactParams(max_depth=4))
    assert table(deeper)[-1] == (4, "file", "high", "backend/app/main.py")
    assert not deeper.beyond_depth


def test_golden_blind_spots(graph: DependencyGraph) -> None:
    spots = {
        s.file_key: (s.extraction_status, dict(s.unresolved_imports))
        for s in impact(graph, [DB]).blind_spots
    }
    assert spots == {
        "backend/app/legacy.py": ("failed", {}),
        "backend/app/plugins.py": (
            "full",
            {UnresolvedReason.DYNAMIC: 1, UnresolvedReason.NOT_FOUND: 1},
        ),
    }


def test_symbol_seed_stays_precise(graph: DependencyGraph) -> None:
    """Changing UserService affects its dependents, not those of the file's other
    symbols; the file is listed as changed at distance 0."""
    assert table(impact(graph, [USER_SERVICE])) == [
        (0, "file", "high", SERVICE),
        (1, "file", "high", "backend/app/users/routes.py"),
        (1, "test", "high", "backend/tests/test_users.py"),
        (1, "endpoint", "medium", ENDPOINT),
        (2, "file", "high", "backend/app/main.py"),
    ]


def test_external_package_and_directory_seeds(graph: DependencyGraph) -> None:
    upgraded = impact(graph, ["ext:pypi:sqlalchemy"], ImpactParams(max_depth=1))
    assert [(i.distance, i.key) for i in upgraded.items] == [
        (1, DB),
        (1, "backend/app/users/models.py"),
    ]
    directory = impact(graph, ["backend/app/users"], ImpactParams(max_depth=0))
    assert {i.key for i in directory.items if i.distance == 0} == {
        "backend/app/users/__init__.py",
        "backend/app/users/models.py",
        "backend/app/users/routes.py",
        "backend/app/users/schemas.py",
        SERVICE,
        ENDPOINT,
    }


def test_analyzer_output(analyzed_graph: DependencyGraph) -> None:
    """IMPORTS only: the same files, no endpoint (no HANDLES), all high."""
    assert table(impact(analyzed_graph, [DB])) == [
        (0, "file", "high", DB),
        (1, "file", "high", "backend/app/users/models.py"),
        (2, "file", "high", SERVICE),
        (3, "file", "high", "backend/app/users/routes.py"),
        (3, "test", "high", "backend/tests/test_users.py"),
    ]


# ---------------------------------------------------------------------------
# Synthetic graphs: rules and bounds


def files(*names: str) -> list[GraphNode]:
    return [
        GraphNode("/", NodeKind.REPOSITORY, "r", None, None),
        *(GraphNode(n, NodeKind.FILE, n, "/", None) for n in names),
    ]


def imports(source: str, target: str, basis: Basis = Basis.RESOLVED) -> GraphEdge:
    return GraphEdge(EdgeKind.IMPORTS, source, target, basis)


def test_cycles_terminate() -> None:
    g = DependencyGraph(
        files("a.py", "b.py", "c.py"),
        [imports("b.py", "a.py"), imports("c.py", "b.py"), imports("a.py", "c.py")],
    )
    result = impact(g, ["a.py"], ImpactParams(max_depth=10))
    assert [(i.distance, i.key) for i in result.items] == [(0, "a.py"), (1, "b.py"), (2, "c.py")]
    assert not result.beyond_depth  # c -> a leads back to a seed


def test_depth_and_node_bounds() -> None:
    chain = [f"f{i}.py" for i in range(6)]
    g = DependencyGraph(files(*chain), [imports(chain[i + 1], chain[i]) for i in range(5)])
    shallow = impact(g, ["f0.py"], ImpactParams(max_depth=2))
    assert [i.key for i in shallow.items] == ["f0.py", "f1.py", "f2.py"]
    assert shallow.beyond_depth and not shallow.truncated
    only_seeds = impact(g, ["f0.py"], ImpactParams(max_depth=0))
    assert [i.key for i in only_seeds.items] == ["f0.py"] and only_seeds.beyond_depth
    capped = impact(g, ["f0.py"], ImpactParams(max_depth=10, max_nodes=3))
    assert capped.truncated and not capped.beyond_depth
    assert [i.key for i in capped.items] == ["f0.py", "f1.py", "f2.py"]


def test_most_confident_shortest_path_wins() -> None:
    """r reaches s in two hops through `a` (heuristic) or `z` (resolved): the resolved
    path wins although `a` sorts first, so r is high confidence."""
    g = DependencyGraph(
        files("s.py", "a.py", "z.py", "r.py"),
        [
            imports("a.py", "s.py", Basis.HEURISTIC),
            imports("z.py", "s.py"),
            imports("r.py", "a.py"),
            imports("r.py", "z.py"),
        ],
    )
    r = next(i for i in impact(g, ["s.py"]).items if i.key == "r.py")
    assert r.confidence is Confidence.HIGH
    assert [(e.source_key, e.target_key) for e in r.via] == [("z.py", "s.py"), ("r.py", "z.py")]
    a = next(i for i in impact(g, ["s.py"]).items if i.key == "a.py")
    assert a.confidence is Confidence.MEDIUM


@pytest.mark.parametrize("heuristic", ["A", "B"])
def test_file_reached_through_two_symbols_takes_the_most_confident(heuristic: str) -> None:
    """t.py#A and t.py#B both inherit from the seed, one heuristically: t.py is reached
    through the resolved one, whichever of the two symbols sorts first."""
    resolved = "B" if heuristic == "A" else "A"
    nodes = [
        *files("s.py", "t.py"),
        GraphNode("s.py#S", NodeKind.TYPE, "S", "s.py", None),
        GraphNode("t.py#A", NodeKind.TYPE, "A", "t.py", None),
        GraphNode("t.py#B", NodeKind.TYPE, "B", "t.py", None),
    ]
    g = DependencyGraph(
        nodes,
        [
            GraphEdge(EdgeKind.INHERITS, f"t.py#{heuristic}", "s.py#S", Basis.HEURISTIC),
            GraphEdge(EdgeKind.INHERITS, f"t.py#{resolved}", "s.py#S", Basis.RESOLVED),
        ],
    )
    t = next(i for i in impact(g, ["s.py#S"]).items if i.key == "t.py")
    assert (t.distance, t.confidence) == (1, Confidence.HIGH)
    assert t.via == (EdgeRef.of(EdgeKind.INHERITS, f"t.py#{resolved}", "s.py#S"),)


def test_tests_edges_and_their_switch() -> None:
    g = DependencyGraph(
        files("lib.py", "tests/check.py"),
        [GraphEdge(EdgeKind.TESTS, "tests/check.py", "lib.py", Basis.HEURISTIC)],
    )
    found = impact(g, ["lib.py"]).items
    assert [(i.key, i.category, i.confidence.value) for i in found] == [
        ("lib.py", "file", "high"),
        ("tests/check.py", "test", "medium"),
    ]
    assert [i.key for i in impact(g, ["lib.py"], ImpactParams(follow_tests=False)).items] == [
        "lib.py"
    ]


def test_seed_errors(graph: DependencyGraph) -> None:
    with pytest.raises(ImpactError, match="unknown"):
        impact(graph, ["backend/nope.py"])
    with pytest.raises(ImpactError, match="repository root"):
        impact(graph, ["/"])
    with pytest.raises(ImpactError, match="at least one"):
        impact(graph, [])
    with pytest.raises(ImpactError, match="at most 1"):
        impact(graph, [DB, SERVICE], ImpactParams(max_seeds=1))


# ---------------------------------------------------------------------------
# Witness paths, determinism, construction


def check_paths(graph: DependencyGraph, seeds: list[str], result: ImpactResult) -> None:
    edges = {(e.kind, e.source_key, e.target_key): e for e in graph.edges}
    starts = {k for s in seeds for k in graph.descendants(s)}
    starts |= {f for s in seeds if (f := graph.file_of(s)) is not None}
    for item in result.items:
        hops = [e for e in item.via if e.kind is not EdgeKind.HANDLES]
        assert len(hops) == item.distance, item.key
        if not item.via:
            assert item.distance == 0 and item.key in starts
            continue
        assert all((e.kind, e.source_key, e.target_key) in edges for e in item.via)
        assert item.via[0].target_key in starts or item.via[0].kind is EdgeKind.HANDLES
        for previous, step in zip(item.via, item.via[1:], strict=False):
            # Each hop starts where the previous one ended (same file: file-level reach).
            reached = (
                previous.target_key if previous.kind is EdgeKind.HANDLES else previous.source_key
            )
            anchor = step.source_key if step.kind is EdgeKind.HANDLES else step.target_key
            assert graph.file_of(reached) == graph.file_of(anchor) or reached == anchor
        last: EdgeRef = item.via[-1]
        ended = last.target_key if last.kind is EdgeKind.HANDLES else last.source_key
        assert ended == item.key or graph.file_of(ended) == item.key


@pytest.mark.parametrize(
    "seed",
    [DB, USER_SERVICE, "backend/app/users/models.py#User", "ext:pypi:fastapi", "backend/app"],
)
def test_witness_paths_are_real_and_connected(graph: DependencyGraph, seed: str) -> None:
    check_paths(graph, [seed], impact(graph, [seed], ImpactParams(max_depth=10)))


def test_order_of_facts_does_not_matter(golden: SnapshotDocument, graph: DependencyGraph) -> None:
    rng = random.Random(7)
    update = {}
    for name in ("nodes", "edges", "classifications", "unresolved_references"):
        items = list(getattr(golden, name))
        rng.shuffle(items)
        update[name] = tuple(items)
    shuffled = SnapshotDocument.model_validate(golden.model_copy(update=update).model_dump())
    other = DependencyGraph.from_document(shuffled)
    assert other.parts() == graph.parts()
    assert impact(other, [DB, USER_SERVICE]) == impact(graph, [DB, USER_SERVICE])


def test_records_build_the_same_graph(graph: DependencyGraph) -> None:
    """from_parts (what the database loader feeds) equals from_document."""
    nodes, edges, roles, unresolved = graph.parts()
    rebuilt = DependencyGraph.from_parts(
        [
            (
                n.key,
                n.kind.value,
                n.name,
                n.parent_key,
                None if n.extraction_status is None else n.extraction_status.value,
            )
            for n in nodes
        ],
        [(e.kind.value, e.source_key, e.target_key, e.basis.value) for e in edges],
        [(k, r.value) for k, r in roles],
        [(k, r.value) for k, r in unresolved],
    )
    assert rebuilt.parts() == graph.parts()


# ---------------------------------------------------------------------------
# Performance (8 000 nodes)


@pytest.mark.slow
def test_impact_on_a_large_snapshot(tmp_path: Path) -> None:
    from ..api.test_large_snapshot_db import SHARED, generate

    doc = analyze(
        generate(tmp_path),
        repository=RepositoryRef(host="local", owner="t", name="l"),
        commit_sha="a" * 40,
    )
    assert len(doc.nodes) > 8000
    started = time.perf_counter()
    g = DependencyGraph.from_document(doc)
    result = impact(g, [SHARED], ImpactParams(max_depth=10))
    elapsed = time.perf_counter() - started
    assert sum(i.category == "file" for i in result.items) == 2001  # hub.py + 2 000 importers
    assert not result.truncated and not result.beyond_depth
    check_paths(g, [SHARED], result)
    assert elapsed < 2.0, elapsed
