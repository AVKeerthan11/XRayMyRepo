"""A compact, immutable dependency graph of one snapshot, for traversals.

Built either from a ``SnapshotDocument`` or from plain records (what persistence
loads: ``from_parts``), so in-memory and database-loaded graphs are the same object
and can be compared (``parts``). Holds what impact traversal needs and nothing more:
node identity and containment, every edge with its basis, roles, and what the
extractors could not see (extraction status, unresolved imports).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

from xraymyrepo.cim import (
    Basis,
    ClassificationFacet,
    EdgeKind,
    EdgeRef,
    ExtractionStatus,
    NodeKind,
    ReferenceKind,
    Role,
    SnapshotDocument,
    UnresolvedReason,
)


@dataclass(frozen=True)
class GraphNode:
    key: str
    kind: NodeKind
    name: str
    parent_key: str | None
    extraction_status: ExtractionStatus | None


@dataclass(frozen=True)
class GraphEdge:
    kind: EdgeKind
    source_key: str
    target_key: str
    basis: Basis

    @property
    def ref(self) -> EdgeRef:
        return EdgeRef.of(self.kind, self.source_key, self.target_key)

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.kind.value, self.source_key, self.target_key)


GraphParts = tuple[
    tuple[GraphNode, ...],
    tuple[GraphEdge, ...],
    tuple[tuple[str, Role], ...],
    tuple[tuple[str, UnresolvedReason], ...],
]


class DependencyGraph:
    def __init__(
        self,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
        roles: Iterable[tuple[str, Role]] = (),
        unresolved_imports: Iterable[tuple[str, UnresolvedReason]] = (),
    ) -> None:
        self.nodes: dict[str, GraphNode] = {n.key: n for n in sorted(nodes, key=lambda n: n.key)}
        self.edges: tuple[GraphEdge, ...] = tuple(sorted(edges, key=lambda e: e.sort_key))
        self._roles = tuple(sorted(set(roles)))
        self._unresolved = tuple(sorted(unresolved_imports))

    # -- construction --------------------------------------------------------

    @classmethod
    def from_document(cls, doc: SnapshotDocument) -> DependencyGraph:
        return cls(
            nodes=(
                GraphNode(n.key, n.kind, n.name, n.parent_key, n.extraction_status)
                for n in doc.nodes
            ),
            edges=(GraphEdge(e.kind, e.source_key, e.target_key, e.basis) for e in doc.edges),
            roles=(
                (c.node_key, Role(c.value))
                for c in doc.classifications
                if c.facet is ClassificationFacet.ROLE
            ),
            unresolved_imports=(
                (u.file_key, u.reason)
                for u in doc.unresolved_references
                if u.ref_kind is ReferenceKind.IMPORT
            ),
        )

    @classmethod
    def from_parts(
        cls,
        nodes: Iterable[tuple[str, str, str, str | None, str | None]],
        edges: Iterable[tuple[str, str, str, str]],
        roles: Iterable[tuple[str, str]],
        unresolved_imports: Iterable[tuple[str, str]],
    ) -> DependencyGraph:
        """From plain records: (key, kind, name, parent key, extraction status),
        (kind, source key, target key, basis), (node key, role), (file key, reason)."""
        return cls(
            nodes=(
                GraphNode(
                    key,
                    NodeKind(kind),
                    name,
                    parent,
                    None if status is None else ExtractionStatus(status),
                )
                for key, kind, name, parent, status in nodes
            ),
            edges=(
                GraphEdge(EdgeKind(kind), source, target, Basis(basis))
                for kind, source, target, basis in edges
            ),
            roles=((key, Role(role)) for key, role in roles),
            unresolved_imports=(
                (key, UnresolvedReason(reason)) for key, reason in unresolved_imports
            ),
        )

    def parts(self) -> GraphParts:
        """Everything the graph holds, in canonical order (for comparisons)."""
        return (tuple(self.nodes.values()), self.edges, self._roles, self._unresolved)

    # -- lookups -------------------------------------------------------------

    @cached_property
    def children(self) -> dict[str, tuple[str, ...]]:
        found: dict[str, list[str]] = {}
        for node in self.nodes.values():
            if node.parent_key is not None:
                found.setdefault(node.parent_key, []).append(node.key)
        return {key: tuple(sorted(values)) for key, values in found.items()}

    def descendants(self, key: str) -> list[str]:
        """The node and everything declared inside it, in key order."""
        found, stack = [], [key]
        while stack:
            current = stack.pop()
            found.append(current)
            stack.extend(self.children.get(current, ()))
        return sorted(found)

    def file_of(self, key: str) -> str | None:
        """The containing file: itself for files, the declaring file for symbols, the
        registration file for endpoints; None for anything else."""
        node = self.nodes[key]
        while node.kind in (NodeKind.TYPE, NodeKind.FUNCTION, NodeKind.ENDPOINT):
            assert node.parent_key is not None
            node = self.nodes[node.parent_key]
        return node.key if node.kind is NodeKind.FILE else None

    @cached_property
    def incoming(self) -> dict[str, tuple[GraphEdge, ...]]:
        found: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            found.setdefault(edge.target_key, []).append(edge)
        return {key: tuple(values) for key, values in found.items()}

    @cached_property
    def outgoing(self) -> dict[str, tuple[GraphEdge, ...]]:
        found: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            found.setdefault(edge.source_key, []).append(edge)
        return {key: tuple(values) for key, values in found.items()}

    @cached_property
    def roles(self) -> dict[str, frozenset[Role]]:
        found: dict[str, set[Role]] = {}
        for key, role in self._roles:
            found.setdefault(key, set()).add(role)
        return {key: frozenset(values) for key, values in found.items()}

    @cached_property
    def unresolved_imports(self) -> dict[str, Counter[UnresolvedReason]]:
        """Unresolved import references per file, by reason."""
        found: dict[str, Counter[UnresolvedReason]] = {}
        for key, reason in self._unresolved:
            found.setdefault(key, Counter())[reason] += 1
        return found
