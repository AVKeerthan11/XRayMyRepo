"""Per-process cache of snapshot dependency graphs, for on-request traversals.

A complete snapshot never changes, so a graph cached by snapshot id can never be
stale. The cache is bounded (least recently used graphs are dropped first) and
thread-safe: request handlers run in a thread pool. Two concurrent misses for one
snapshot may both load it; the second simply replaces the first.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from xraymyrepo.graph import DependencyGraph
from xraymyrepo.persistence import queries

from .db import Connection


class GraphCache:
    def __init__(self, size: int) -> None:
        self.size = size
        self.hits = 0
        self.misses = 0
        self._graphs: OrderedDict[int, DependencyGraph] = OrderedDict()
        self._lock = Lock()

    def get(self, snapshot_id: int, load: Callable[[], DependencyGraph]) -> DependencyGraph:
        with self._lock:
            graph = self._graphs.get(snapshot_id)
            if graph is not None:
                self._graphs.move_to_end(snapshot_id)
                self.hits += 1
                return graph
            self.misses += 1
        graph = load()  # outside the lock: other snapshots are not blocked meanwhile
        if self.size:
            with self._lock:
                self._graphs[snapshot_id] = graph
                self._graphs.move_to_end(snapshot_id)
                while len(self._graphs) > self.size:
                    self._graphs.popitem(last=False)
        return graph

    def __len__(self) -> int:
        return len(self._graphs)


def snapshot_graph(conn: Connection, cache: GraphCache, snapshot_id: int) -> DependencyGraph:
    """The snapshot's dependency graph, loaded (four queries) on a cache miss."""

    def load() -> DependencyGraph:
        records = queries.load_graph_records(conn, snapshot_id)
        return DependencyGraph.from_parts(
            records.nodes, records.edges, records.roles, records.unresolved_imports
        )

    return cache.get(snapshot_id, load)
