"""Lookups over one snapshot document, shared by the derivation steps.

Everything is built in canonical order (``SnapshotDocument.canonical()``), so every
step that iterates over these collections is deterministic.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from functools import cached_property

from xraymyrepo.cim import (
    ClassificationFacet,
    Edge,
    NodeKind,
    Origin,
    Role,
    SnapshotDocument,
)
from xraymyrepo.cim.enums import EXTRACTED_STATUSES
from xraymyrepo.cim.keys import decode_path
from xraymyrepo.cim.nodes import ExternalPackageAttributes, FileAttributes, Node

from .params import ArchitectureParams


@dataclass(frozen=True)
class FileEdge:
    """A snapshot edge seen between the files that contain its two ends."""

    edge: Edge
    source_file: str
    target_file: str


class SnapshotIndex:
    def __init__(self, doc: SnapshotDocument, params: ArchitectureParams) -> None:
        self.doc = doc.canonical()
        self.params = params
        self.nodes = self.doc.nodes_by_key

    # -- structure -----------------------------------------------------------

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

    def module_name(self, file: Node) -> str | None:
        attrs = file.attributes
        return attrs.module_name if isinstance(attrs, FileAttributes) else None

    # -- scopes --------------------------------------------------------------

    def is_test(self, file: Node) -> bool:
        if any(role is Role.TEST_FILE for role, _ in self.roles.get(file.key, ())):
            return True
        *directories, name = decode_path(file.key).split("/")
        return any(d in self.params.test_directories for d in directories) or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in self.params.test_file_patterns
        )

    def _has_origin(self, file: Node, allowed: tuple[Origin, ...]) -> bool:
        origins = self.origins.get(file.key, frozenset())
        return bool(origins) and origins <= set(allowed)

    @cached_property
    def layer_scope(self) -> tuple[Node, ...]:
        """Files considered for layering: extracted source files of the layer languages,
        excluding tests (and so generated and vendored code)."""
        p = self.params
        return tuple(
            f for f in self.files
            if f.language in p.layer_languages
            and f.extraction_status in EXTRACTED_STATUSES
            and self._has_origin(f, p.layer_origins)
            and not self.is_test(f)
        )  # fmt: skip

    @cached_property
    def component_scope(self) -> tuple[Node, ...]:
        """Files assigned to components: modules (with a module name) of the component
        languages and origins, whatever their extraction status."""
        p = self.params
        return tuple(
            f for f in self.files
            if f.language in p.component_languages
            and self._has_origin(f, p.component_origins)
            and self.module_name(f) is not None
        )  # fmt: skip

    # -- edges ---------------------------------------------------------------

    @cached_property
    def file_edges(self) -> tuple[FileEdge, ...]:
        """Every edge whose two ends lie in two different files, in canonical order."""
        found = []
        for edge in self.doc.edges:
            source, target = self.file_of(edge.source_key), self.file_of(edge.target_key)
            if source is not None and target is not None and source != target:
                found.append(FileEdge(edge, source, target))
        return tuple(found)

    @cached_property
    def dependency_edges(self) -> tuple[FileEdge, ...]:
        kinds = set(self.params.dependency_kinds)
        return tuple(e for e in self.file_edges if e.edge.kind in kinds)

    @cached_property
    def external_dependencies(self) -> dict[str, tuple[tuple[Edge, Node], ...]]:
        """Per file: dependency edges from the file (or its symbols) to external packages."""
        kinds = set(self.params.dependency_kinds)
        found: dict[str, list[tuple[Edge, Node]]] = {}
        for edge in self.doc.edges:
            target = self.nodes[edge.target_key]
            if edge.kind not in kinds or target.kind is not NodeKind.EXTERNAL_PACKAGE:
                continue
            source_file = self.file_of(edge.source_key)
            if source_file is not None:
                found.setdefault(source_file, []).append((edge, target))
        return {key: tuple(values) for key, values in found.items()}

    @staticmethod
    def package_name(node: Node) -> str:
        assert isinstance(node.attributes, ExternalPackageAttributes)
        return node.attributes.package_name
