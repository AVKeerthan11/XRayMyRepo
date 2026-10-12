"""File-level view of one snapshot, shared by the derivation algorithms.

Built in canonical order (``SnapshotDocument.canonical()``), so everything that
iterates over it is deterministic.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

from xraymyrepo.cim import (
    ClassificationFacet,
    Edge,
    EdgeKind,
    NodeKind,
    Origin,
    Role,
    SnapshotDocument,
)
from xraymyrepo.cim.keys import decode_path
from xraymyrepo.cim.nodes import Node


@dataclass(frozen=True)
class FileEdge:
    """A snapshot edge seen between the files that contain its two ends."""

    edge: Edge
    source_file: str
    target_file: str


class FileGraph:
    def __init__(self, doc: SnapshotDocument) -> None:
        self.doc = doc.canonical()
        self.nodes = self.doc.nodes_by_key

    @cached_property
    def files(self) -> tuple[Node, ...]:
        return tuple(n for n in self.doc.nodes if n.kind is NodeKind.FILE)

    def file_of(self, key: str) -> str | None:
        """The file that contains a node: itself for files, the declaring file for
        symbols, the registration file for endpoints; None for anything else."""
        node = self.nodes[key]
        while node.kind in (NodeKind.TYPE, NodeKind.FUNCTION, NodeKind.ENDPOINT):
            assert node.parent_key is not None
            node = self.nodes[node.parent_key]
        return node.key if node.kind is NodeKind.FILE else None

    @cached_property
    def origins(self) -> dict[str, frozenset[Origin]]:
        found: dict[str, set[Origin]] = {}
        for c in self.doc.classifications:
            if c.facet is ClassificationFacet.ORIGIN:
                found.setdefault(c.node_key, set()).add(Origin(c.value))
        return {key: frozenset(values) for key, values in found.items()}

    def has_origin(self, file: Node, allowed: Iterable[Origin]) -> bool:
        """True when the file has origins and all of them are allowed."""
        origins = self.origins.get(file.key, frozenset())
        return bool(origins) and origins <= set(allowed)

    @cached_property
    def roles(self) -> dict[str, tuple[tuple[Role, str], ...]]:
        """(role, classified node key) for each file, including roles of its symbols."""
        found: dict[str, list[tuple[Role, str]]] = {}
        for c in self.doc.classifications:
            if c.facet is ClassificationFacet.ROLE:
                file_key = self.file_of(c.node_key)
                if file_key is not None:
                    found.setdefault(file_key, []).append((Role(c.value), c.node_key))
        return {key: tuple(values) for key, values in found.items()}

    def is_test(
        self, file: Node, test_directories: Iterable[str], test_file_patterns: Iterable[str]
    ) -> bool:
        """A ``test_file`` role, a directory named like a test directory, or a test file name."""
        if any(role is Role.TEST_FILE for role, _ in self.roles.get(file.key, ())):
            return True
        *directories, name = decode_path(file.key).split("/")
        return any(d in set(test_directories) for d in directories) or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in test_file_patterns
        )

    @cached_property
    def file_edges(self) -> tuple[FileEdge, ...]:
        """Every edge whose two ends lie in two different files, in canonical order."""
        found = []
        for edge in self.doc.edges:
            source, target = self.file_of(edge.source_key), self.file_of(edge.target_key)
            if source is not None and target is not None and source != target:
                found.append(FileEdge(edge, source, target))
        return tuple(found)

    def edges_of_kinds(self, kinds: Iterable[EdgeKind]) -> tuple[FileEdge, ...]:
        wanted = set(kinds)
        return tuple(e for e in self.file_edges if e.edge.kind in wanted)
