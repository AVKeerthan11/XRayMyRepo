"""Change impact: if these nodes change, what is affected? Pure and deterministic.

A static, evidence-backed estimate over one snapshot's graph, computed on request
(nothing is stored):

* **Seeds** are node keys (any kind but the repository). A seed stands for itself and
  everything declared inside it: a file seed covers its symbols, a directory seed its
  files. A symbol seed stays precise (its file's other symbols are not changed), but
  the file is listed as changed at distance 0 and file-level imports of it are
  followed.
* **Traversal**, level by level (breadth first):
    - dependency edges (``IMPORTS``, ``INHERITS``, ``CALLS``) backwards: whoever depends
      on an affected node is affected, one hop further;
    - ``TESTS`` backwards: the tests of an affected node are affected, one hop further;
    - ``HANDLES`` forwards at the **same** distance: an endpoint is affected exactly
      when its handler is.
  A reached dependent counts at **file level** (imports are module-level facts): once
  any node of a file is reached, the whole file and its symbols are affected.
* **Bounds**: ``max_depth`` (hops) and ``max_nodes`` (reached nodes). Hitting the node
  bound sets ``truncated``; stopping at the depth bound with dependents left sets
  ``beyond_depth``.
* **Witness path and confidence**: every result carries one shortest path of real
  edges from a seed. Among equally short paths the most confident wins (all edges
  ``resolved``/``observed``: ``high``; any ``heuristic`` edge: ``medium``), then
  edge order (kind, source key, target key). Deterministic.
* **Blind spots** are reported, never added: files with unresolved ``dynamic`` or
  ``not_found`` imports could depend on the seeds unseen, and files whose
  extraction ``failed`` or was ``partial`` have unknown dependencies.

Results are affected **files** (category ``file`` or ``test``) and **endpoints**.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xraymyrepo.cim import (
    Basis,
    Confidence,
    EdgeKind,
    EdgeRef,
    ExtractionStatus,
    NodeKind,
    Role,
    UnresolvedReason,
)
from xraymyrepo.cim.keys import decode_path
from xraymyrepo.graph import DependencyGraph, GraphEdge

Category = Literal["file", "test", "endpoint"]

_HIDING_REASONS = (UnresolvedReason.DYNAMIC, UnresolvedReason.NOT_FOUND)
_UNPARSED = (ExtractionStatus.FAILED, ExtractionStatus.PARTIAL)
_RANK = {Confidence.HIGH: 1, Confidence.MEDIUM: 0}


class ImpactParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_depth: int = Field(default=3, ge=0, le=10)
    max_nodes: int = Field(default=10_000, ge=1, le=100_000)
    max_seeds: int = Field(default=50, ge=1)
    dependency_kinds: tuple[EdgeKind, ...] = (EdgeKind.CALLS, EdgeKind.IMPORTS, EdgeKind.INHERITS)
    follow_tests: bool = True
    follow_handles: bool = True
    test_directories: tuple[str, ...] = ("test", "tests")
    test_file_patterns: tuple[str, ...] = ("*_test.py", "conftest.py", "test_*.py")


class ImpactError(ValueError):
    """Seeds that cannot be traversed from (unknown, the repository, too many)."""


@dataclass(frozen=True)
class ImpactedNode:
    key: str
    kind: NodeKind
    name: str
    category: Category
    distance: int
    confidence: Confidence
    via: tuple[EdgeRef, ...]
    """A shortest path of real edges from a seed (empty at distance 0)."""


@dataclass(frozen=True)
class BlindSpot:
    file_key: str
    extraction_status: ExtractionStatus | None
    unresolved_imports: dict[UnresolvedReason, int]
    """Unresolved ``dynamic`` / ``not_found`` imports by reason."""


@dataclass(frozen=True)
class ImpactResult:
    seeds: tuple[str, ...]
    max_depth: int
    items: tuple[ImpactedNode, ...]
    """Affected files and endpoints, by (distance, key)."""
    truncated: bool
    """The node bound stopped the traversal: there may be more affected nodes."""
    beyond_depth: bool
    """Dependents exist beyond ``max_depth``."""
    blind_spots: tuple[BlindSpot, ...]


@dataclass
class _Reach:
    distance: int
    confidence: Confidence
    edge: GraphEdge | None
    previous: str | None


def _edge_confidence(edge: GraphEdge) -> Confidence:
    return Confidence.MEDIUM if edge.basis is Basis.HEURISTIC else Confidence.HIGH


def _weaker(a: Confidence, b: Confidence) -> Confidence:
    return a if _RANK[a] <= _RANK[b] else b


def blind_spots(graph: DependencyGraph) -> tuple[BlindSpot, ...]:
    """Files whose dependencies the snapshot cannot fully show (seed-independent)."""
    found = []
    for node in graph.nodes.values():
        if node.kind is not NodeKind.FILE:
            continue
        reasons = graph.unresolved_imports.get(node.key, Counter())
        hiding = {r: reasons[r] for r in _HIDING_REASONS if reasons.get(r)}
        if hiding or node.extraction_status in _UNPARSED:
            found.append(BlindSpot(node.key, node.extraction_status, hiding))
    return tuple(found)


class _Traversal:
    def __init__(self, graph: DependencyGraph, params: ImpactParams) -> None:
        self.g = graph
        self.p = params
        self.reached: dict[str, _Reach] = {}
        self.truncated = False

    def add(self, key: str, reach: _Reach) -> bool:
        if key in self.reached:
            return False
        if len(self.reached) >= self.p.max_nodes:
            self.truncated = True
            return False
        self.reached[key] = reach
        return True

    def lift(self, entry: str) -> list[str]:
        """Mark the file of a reached node, and everything declared in it, affected at
        the entry's distance and confidence (endpoints only come through HANDLES)."""
        reach = self.reached[entry]
        file_key = self.g.file_of(entry)
        added = []
        for key in self.g.descendants(file_key) if file_key is not None else [entry]:
            if key != entry and self.g.nodes[key].kind is NodeKind.ENDPOINT:
                continue
            if self.add(key, _Reach(reach.distance, reach.confidence, None, entry)):
                added.append(key)
        return added

    def handles(self, keys: Sequence[str]) -> list[str]:
        """Endpoints handled by newly reached handlers, at the handler's distance."""
        added: list[str] = []
        if not self.p.follow_handles:
            return added
        for key in sorted(keys):
            for edge in self.g.outgoing.get(key, ()):
                if edge.kind is EdgeKind.HANDLES:
                    reach = self.reached[key]
                    confidence = _weaker(reach.confidence, _edge_confidence(edge))
                    if self.add(edge.target_key, _Reach(reach.distance, confidence, edge, key)):
                        added.append(edge.target_key)
        return added

    def follows(self, edge: GraphEdge) -> bool:
        return edge.kind in self.p.dependency_kinds or (
            self.p.follow_tests and edge.kind is EdgeKind.TESTS
        )

    def run(self, seeds: Sequence[str]) -> tuple[list[str], bool]:
        frontier: list[str] = []
        for seed in seeds:
            for key in self.g.descendants(seed):
                if self.add(key, _Reach(0, Confidence.HIGH, None, None if key == seed else seed)):
                    frontier.append(key)
            file_key = self.g.file_of(seed)
            # The changed file: listed, and file-level imports of it followed, without
            # marking its other symbols changed.
            if file_key is not None and self.add(file_key, _Reach(0, Confidence.HIGH, None, seed)):
                frontier.append(file_key)
        frontier += self.handles(frontier)
        distance = 0
        while frontier and distance < self.p.max_depth and not self.truncated:
            best: dict[str, tuple[int, tuple[str, str, str], GraphEdge, str]] = {}
            for key in sorted(frontier):
                for edge in self.g.incoming.get(key, ()):
                    if not self.follows(edge) or edge.source_key in self.reached:
                        continue
                    confidence = _weaker(self.reached[key].confidence, _edge_confidence(edge))
                    candidate = (-_RANK[confidence], edge.sort_key, edge, key)
                    current = best.get(edge.source_key)
                    if current is None or candidate[:2] < current[:2]:
                        best[edge.source_key] = candidate
            distance += 1
            frontier = []
            for source in sorted(best):
                rank, _, edge, previous = best[source]
                confidence = Confidence.HIGH if rank == -1 else Confidence.MEDIUM
                if self.add(source, _Reach(distance, confidence, edge, previous)):
                    frontier.append(source)
            # Every node reached at this level keeps its own path; only then are files
            # marked, from the most confident entry first (then edge order), so a file
            # reached through several of its symbols takes the best of their paths.
            for source in sorted(frontier, key=lambda s: best[s][:2]):
                frontier += self.lift(source)
            frontier += self.handles(frontier)
        beyond = any(
            self.follows(edge) and edge.source_key not in self.reached
            for key in frontier
            for edge in self.g.incoming.get(key, ())
        )
        return frontier, beyond and not self.truncated

    def path(self, key: str) -> tuple[EdgeRef, ...]:
        edges = []
        current: str | None = key
        while current is not None:
            reach = self.reached[current]
            if reach.edge is not None:
                edges.append(reach.edge.ref)
            current = reach.previous
        return tuple(reversed(edges))


def impact(
    graph: DependencyGraph, seeds: Sequence[str], params: ImpactParams | None = None
) -> ImpactResult:
    params = params or ImpactParams()
    unique = sorted(set(seeds))
    if not unique:
        raise ImpactError("at least one seed is required")
    if len(unique) > params.max_seeds:
        raise ImpactError(f"at most {params.max_seeds} seeds")
    for seed in unique:
        node = graph.nodes.get(seed)
        if node is None:
            raise ImpactError(f"unknown node {seed!r}")
        if node.kind is NodeKind.REPOSITORY:
            raise ImpactError("the repository root cannot be a seed")
    traversal = _Traversal(graph, params)
    _, beyond = traversal.run(unique)

    items = []
    for key, reach in traversal.reached.items():
        node = graph.nodes[key]
        if node.kind not in (NodeKind.FILE, NodeKind.ENDPOINT):
            continue
        category: Category = "endpoint" if node.kind is NodeKind.ENDPOINT else "file"
        if category == "file" and _is_test(graph, key, params):
            category = "test"
        items.append(
            ImpactedNode(
                key,
                node.kind,
                node.name,
                category,
                reach.distance,
                reach.confidence,
                traversal.path(key),
            )
        )
    items.sort(key=lambda item: (item.distance, item.key))
    return ImpactResult(
        seeds=tuple(unique),
        max_depth=params.max_depth,
        items=tuple(items),
        truncated=traversal.truncated,
        beyond_depth=beyond,
        blind_spots=blind_spots(graph),
    )


def _is_test(graph: DependencyGraph, file_key: str, params: ImpactParams) -> bool:
    if any(Role.TEST_FILE in graph.roles.get(k, frozenset()) for k in (file_key,)):
        return True
    *directories, name = decode_path(file_key).split("/")
    return any(d in params.test_directories for d in directories) or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in params.test_file_patterns
    )
