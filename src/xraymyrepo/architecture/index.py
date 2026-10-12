"""Lookups over one snapshot document for the architecture derivation.

The generic file-level view (files, containing file, origins, roles, edges between
files) is ``xraymyrepo.graph.FileGraph``; this adds what the architecture
parameters define (scopes, dependency kinds, external dependencies).
"""

from __future__ import annotations

from functools import cached_property

from xraymyrepo.cim import Edge, NodeKind, SnapshotDocument
from xraymyrepo.cim.enums import EXTRACTED_STATUSES
from xraymyrepo.cim.nodes import ExternalPackageAttributes, FileAttributes, Node
from xraymyrepo.graph import FileEdge, FileGraph

from .params import ArchitectureParams

__all__ = ["FileEdge", "SnapshotIndex"]


class SnapshotIndex(FileGraph):
    def __init__(self, doc: SnapshotDocument, params: ArchitectureParams) -> None:
        super().__init__(doc)
        self.params = params

    def module_name(self, file: Node) -> str | None:
        attrs = file.attributes
        return attrs.module_name if isinstance(attrs, FileAttributes) else None

    # -- scopes --------------------------------------------------------------

    @cached_property
    def layer_scope(self) -> tuple[Node, ...]:
        """Files considered for layering: extracted source files of the layer languages,
        excluding tests (and so generated and vendored code)."""
        p = self.params
        return tuple(
            f for f in self.files
            if f.language in p.layer_languages
            and f.extraction_status in EXTRACTED_STATUSES
            and self.has_origin(f, p.layer_origins)
            and not self.is_test(f, p.test_directories, p.test_file_patterns)
        )  # fmt: skip

    @cached_property
    def component_scope(self) -> tuple[Node, ...]:
        """Files assigned to components: modules (with a module name) of the component
        languages and origins, whatever their extraction status."""
        p = self.params
        return tuple(
            f for f in self.files
            if f.language in p.component_languages
            and self.has_origin(f, p.component_origins)
            and self.module_name(f) is not None
        )  # fmt: skip

    @cached_property
    def dependency_edges(self) -> tuple[FileEdge, ...]:
        return self.edges_of_kinds(self.params.dependency_kinds)

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
