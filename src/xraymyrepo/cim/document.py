"""The snapshot document: the complete, self-checking output of extracting one snapshot.

This is the interchange format between extractors, the database loader and the
golden fixture. It references nodes by key and edges by (kind, source, target);
database ids never appear. Constructing a ``SnapshotDocument`` validates every
cross-record invariant of the CIM v1 contract (see ``_Checker``).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from functools import cached_property

from pydantic import model_validator

from ._base import CIMModel
from .classification import Classification, applicable_kinds
from .coverage import CoverageDeclaration, ExtractionIssue, UnresolvedReference
from .edges import EDGE_RULES, CallsAttributes, Edge, ImportsAttributes, InheritsAttributes
from .enums import (
    EXTRACTED_STATUSES,
    SYMBOL_NODE_KINDS,
    ClassificationFacet,
    EdgeKind,
    ExclusionAction,
    ExtractionStatus,
    IssueSeverity,
    NodeKind,
    Origin,
    Role,
)
from .evidence import ConfigEvidence, DerivedEvidence, SpanEvidence
from .keys import REPOSITORY_KEY, parent_path_key, parse_symbol_key
from .nodes import (
    ECOSYSTEM_LANGUAGES,
    PARENT_KINDS,
    EndpointAttributes,
    Node,
    PackageAttributes,
)
from .provenance import Producer, Provenance
from .snapshot import CIM_SCHEMA_VERSION, AnalysisConfig, SnapshotIdentity


class SnapshotDocument(CIMModel):
    schema_version: str
    snapshot: SnapshotIdentity
    config: AnalysisConfig
    producers: tuple[Producer, ...]
    coverage: tuple[CoverageDeclaration, ...] = ()
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...] = ()
    classifications: tuple[Classification, ...] = ()
    unresolved_references: tuple[UnresolvedReference, ...] = ()
    extraction_issues: tuple[ExtractionIssue, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> SnapshotDocument:
        _Checker(self).run()
        return self

    # -- lookups -------------------------------------------------------------

    @cached_property
    def nodes_by_key(self) -> dict[str, Node]:
        return {node.key: node for node in self.nodes}

    @cached_property
    def edges_by_ref(self) -> dict[tuple[EdgeKind, str, str], Edge]:
        return {(e.kind, e.source_key, e.target_key): e for e in self.edges}

    def node(self, key: str) -> Node:
        return self.nodes_by_key[key]

    def edge(self, kind: EdgeKind, source_key: str, target_key: str) -> Edge:
        return self.edges_by_ref[(kind, source_key, target_key)]

    def edges_of(self, kind: EdgeKind) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.kind is kind)

    def classifications_of(self, node_key: str) -> tuple[Classification, ...]:
        return tuple(c for c in self.classifications if c.node_key == node_key)

    def children(self, key: str) -> tuple[Node, ...]:
        return tuple(n for n in self.nodes if n.parent_key == key)

    # -- canonical form ------------------------------------------------------

    def canonical(self) -> SnapshotDocument:
        """The same document with every collection in a deterministic order."""
        return self.model_copy(
            update={
                "producers": tuple(sorted(self.producers, key=lambda p: p.name)),
                "coverage": tuple(sorted(self.coverage, key=lambda c: c.language)),
                "nodes": tuple(sorted(self.nodes, key=lambda n: n.key)),
                "edges": tuple(
                    sorted(self.edges, key=lambda e: (e.kind, e.source_key, e.target_key))
                ),
                "classifications": tuple(
                    sorted(self.classifications, key=lambda c: (c.node_key, c.facet, c.value))
                ),
                "unresolved_references": tuple(
                    sorted(
                        self.unresolved_references,
                        key=lambda u: (u.file_key, u.start_line, u.start_col, u.raw_text),
                    )
                ),
                "extraction_issues": tuple(
                    sorted(
                        self.extraction_issues,
                        key=lambda i: (
                            i.file_key or "",
                            i.span.start_line if i.span else 0,
                            i.code,
                        ),
                    )
                ),
            }
        )

    def to_canonical_json(self) -> str:
        data = self.canonical().model_dump(mode="json", exclude_defaults=True)
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


class _Checker:
    """Cross-record invariants. Each check raises ``ValueError`` with a precise message."""

    def __init__(self, doc: SnapshotDocument) -> None:
        self.doc = doc
        self.nodes: dict[str, Node] = {}
        self.producer_names: set[str] = set()
        self.origins: dict[str, list[Classification]] = {}

    def run(self) -> None:
        self._header()
        self._nodes()
        self._tree()
        self._edges()
        self._classifications()
        self._packages()
        self._endpoint_scopes()
        self._coverage_and_diagnostics()
        self._provenance()

    # -- header --------------------------------------------------------------

    def _header(self) -> None:
        doc = self.doc
        if doc.schema_version != CIM_SCHEMA_VERSION:
            raise ValueError(f"schema_version {doc.schema_version!r} != {CIM_SCHEMA_VERSION!r}")
        if doc.config.config_hash() != doc.snapshot.config_hash:
            raise ValueError("snapshot.config_hash does not match the hash of config")
        names = [p.name for p in doc.producers]
        _no_duplicates(names, "producer name")
        self.producer_names = set(names)
        _no_duplicates([c.language for c in doc.coverage], "coverage language")

    # -- nodes and containment -----------------------------------------------

    def _nodes(self) -> None:
        _no_duplicates([n.key for n in self.doc.nodes], "node key")
        self.nodes = {n.key: n for n in self.doc.nodes}
        roots = [n for n in self.doc.nodes if n.kind is NodeKind.REPOSITORY]
        if len(roots) != 1:
            raise ValueError("a snapshot has exactly one repository node")

    def _tree(self) -> None:
        for node in self.doc.nodes:
            if node.parent_key is None:
                continue
            parent = self._node(node.parent_key, f"parent of {node.key!r}")
            allowed = PARENT_KINDS[node.kind]
            assert allowed is not None
            if parent.kind not in allowed:
                raise ValueError(f"{node.kind} {node.key!r} cannot be inside a {parent.kind}")
            expected = self._expected_parent_key(node)
            if expected is not None and node.parent_key != expected:
                raise ValueError(
                    f"{node.key!r} must be declared in {expected!r}, not {node.parent_key!r}"
                )
            if node.kind in SYMBOL_NODE_KINDS | {NodeKind.ENDPOINT}:
                file = self._file_of(node)
                if node.language != file.language:
                    raise ValueError(f"{node.key!r} must have the language of {file.key!r}")
                if file.extraction_status not in EXTRACTED_STATUSES:
                    raise ValueError(
                        f"{node.key!r} was extracted from {file.key!r}, whose status is "
                        f"{file.extraction_status}"
                    )

    def _expected_parent_key(self, node: Node) -> str | None:
        if node.kind in (NodeKind.DIRECTORY, NodeKind.FILE):
            return parent_path_key(node.key)
        if node.kind in SYMBOL_NODE_KINDS:
            return parse_symbol_key(node.key).parent_key
        return None  # endpoints: any file (registration site)

    def _file_of(self, node: Node) -> Node:
        current = node
        while current.kind is not NodeKind.FILE:
            assert current.parent_key is not None
            current = self.nodes[current.parent_key]
        return current

    # -- edges ---------------------------------------------------------------

    def _edges(self) -> None:
        _no_duplicates([(e.kind, e.source_key, e.target_key) for e in self.doc.edges], "edge")
        groups: dict[str, list[Edge]] = {}
        for edge in self.doc.edges:
            rule = EDGE_RULES[edge.kind]
            source = self._node(edge.source_key, f"source of {edge.kind}")
            target = self._node(edge.target_key, f"target of {edge.kind}")
            label = f"{edge.kind} {edge.source_key!r} -> {edge.target_key!r}"
            if source.kind not in rule.sources:
                raise ValueError(f"{label}: source cannot be a {source.kind}")
            if target.kind not in rule.targets:
                raise ValueError(f"{label}: target cannot be a {target.kind}")
            attrs = edge.attributes
            if isinstance(attrs, InheritsAttributes | CallsAttributes):
                is_external = target.kind is NodeKind.EXTERNAL_PACKAGE
                if is_external != (attrs.target_symbol is not None):
                    raise ValueError(
                        f"{label}: target_symbol is required for external targets and only there"
                    )
            if isinstance(attrs, ImportsAttributes):
                for via in attrs.via:
                    self._file(via, f"{label} via")
            self._evidence_refs(edge.evidence, label)
            if edge.ambiguity_group is not None:
                groups.setdefault(edge.ambiguity_group, []).append(edge)
            self._source_extracted(source, label)
        for name, members in groups.items():
            if len(members) < 2:
                raise ValueError(f"ambiguity group {name!r} needs at least two candidate edges")
            if len({(e.kind, e.source_key) for e in members}) != 1:
                raise ValueError(f"ambiguity group {name!r} mixes sources or kinds")

    def _source_extracted(self, source: Node, label: str) -> None:
        if source.kind is NodeKind.FILE or source.kind in SYMBOL_NODE_KINDS:
            file = self._file_of(source)
            if file.extraction_status not in EXTRACTED_STATUSES:
                raise ValueError(f"{label}: {file.key!r} was not extracted")

    # -- classifications -----------------------------------------------------

    def _classifications(self) -> None:
        _no_duplicates(
            [(c.node_key, c.facet, c.value) for c in self.doc.classifications], "classification"
        )
        for c in self.doc.classifications:
            label = f"{c.facet}={c.value} on {c.node_key!r}"
            node = self._node(c.node_key, label)
            if node.kind not in applicable_kinds(c.facet, c.value):
                raise ValueError(f"{label}: not applicable to a {node.kind}")
            self._evidence_refs(c.evidence, label)
            if c.facet is ClassificationFacet.ORIGIN:
                self.origins.setdefault(c.node_key, []).append(c)

        policy = self.doc.config.exclusion_policy
        for node in self.doc.nodes:
            if node.kind is not NodeKind.FILE:
                continue
            origins = self.origins.get(node.key, [])
            if not origins:
                raise ValueError(f"file {node.key!r} has no origin classification")
            if len(origins) > 1 and not all(c.contested for c in origins):
                raise ValueError(f"file {node.key!r} has conflicting origins not marked contested")
            if len(origins) == 1:
                action = policy[Origin(origins[0].value)]
                excluded = node.extraction_status is ExtractionStatus.EXCLUDED
                if (action is ExclusionAction.FILE_ONLY) != excluded:
                    raise ValueError(
                        f"file {node.key!r}: origin {origins[0].value} is '{action}' by policy "
                        f"but extraction_status is {node.extraction_status}"
                    )

    def _packages(self) -> None:
        manifest_roles = {
            c.node_key
            for c in self.doc.classifications
            if c.facet is ClassificationFacet.ROLE and c.value == Role.MANIFEST
        }
        for node in self.doc.nodes:
            if not isinstance(node.attributes, PackageAttributes):
                continue
            manifest = self._file(node.attributes.manifest_key, f"manifest of {node.key!r}")
            if manifest.key not in manifest_roles:
                raise ValueError(f"{manifest.key!r} declares a package but is not a manifest")
            root = self._node(node.attributes.root_key, f"root of {node.key!r}")
            if root.kind not in (NodeKind.DIRECTORY, NodeKind.REPOSITORY):
                raise ValueError(f"package root {root.key!r} must be a directory")
            if manifest.parent_key != root.key:
                raise ValueError(f"manifest {manifest.key!r} must sit in the package root")

    def _endpoint_scopes(self) -> None:
        packages = [n for n in self.doc.nodes if isinstance(n.attributes, PackageAttributes)]
        for node in self.doc.nodes:
            if not isinstance(node.attributes, EndpointAttributes):
                continue
            assert node.parent_key is not None and node.span is not None
            expected = endpoint_scope(node.parent_key, node.language, packages)
            if node.attributes.scope_key != expected:
                raise ValueError(
                    f"endpoint {node.key!r} must be scoped to the nearest package enclosing "
                    f"{node.parent_key!r}: {expected!r}"
                )
            self._registration_sites(node)

    def _registration_sites(self, endpoint: Node) -> None:
        """The parent is the earliest registration; the others are span evidence.

        Registration evidence is span evidence produced by the endpoint's own rule
        (``provenance.rule``); other evidence (e.g. a router prefix) may be anywhere.
        """
        assert endpoint.parent_key is not None and endpoint.span is not None
        primary = (endpoint.parent_key, endpoint.span.start_line, endpoint.span.start_col)
        sites = [
            (e.file_key, e.start_line, e.start_col)
            for e in endpoint.evidence
            if isinstance(e, SpanEvidence) and e.provenance.rule == endpoint.provenance.rule
        ]
        if primary not in sites:
            raise ValueError(
                f"endpoint {endpoint.key!r}: its span must be a registration evidence item"
            )
        if min(sites) != primary:
            raise ValueError(
                f"endpoint {endpoint.key!r}: parent_key and span must be the earliest registration "
                f"(file key, line), which is {min(sites)[:2]!r}"
            )

    # -- coverage and diagnostics --------------------------------------------

    def _coverage_and_diagnostics(self) -> None:
        declared = {(c.language, k) for c in self.doc.coverage for k in c.edge_kinds}
        for edge in self.doc.edges:
            language = self._edge_language(edge)
            if language is not None and (language, edge.kind) not in declared:
                raise ValueError(
                    f"{edge.kind} edge from {edge.source_key!r}: snapshot does not declare "
                    f"{edge.kind} coverage for {language!r}"
                )

        for ref in self.doc.unresolved_references:
            label = f"unresolved {ref.ref_kind} {ref.raw_text!r}"
            file = self._file(ref.file_key, label)
            source = self._node(ref.source_key, label)
            if source.kind is not NodeKind.FILE and source.kind not in SYMBOL_NODE_KINDS:
                raise ValueError(f"{label}: source must be a file or symbol")
            if self._file_of(source).key != file.key:
                raise ValueError(f"{label}: source {source.key!r} is not in {file.key!r}")
            if file.extraction_status not in EXTRACTED_STATUSES:
                raise ValueError(f"{label}: {file.key!r} was not extracted")
            for candidate in ref.candidates:
                self._node(candidate, f"{label} candidate")

        issues_by_file = Counter(
            i.file_key for i in self.doc.extraction_issues if i.severity is IssueSeverity.ERROR
        )
        for issue in self.doc.extraction_issues:
            if issue.file_key is not None:
                self._file(issue.file_key, f"issue {issue.code}")
        parents = {n.parent_key for n in self.doc.nodes}
        for node in self.doc.nodes:
            if node.kind is NodeKind.FILE:
                status = node.extraction_status
                needs_issue = status in (ExtractionStatus.FAILED, ExtractionStatus.PARTIAL)
                if needs_issue and not issues_by_file[node.key]:
                    raise ValueError(f"{status} file {node.key!r} needs an error issue")
                if status not in EXTRACTED_STATUSES and node.key in parents:
                    raise ValueError(f"{status} file {node.key!r} cannot contain extracted nodes")

    def _edge_language(self, edge: Edge) -> str | None:
        source = self.nodes[edge.source_key]
        if isinstance(source.attributes, PackageAttributes):
            return self.nodes[source.attributes.manifest_key].language
        return source.language

    # -- provenance ----------------------------------------------------------

    def _provenance(self) -> None:
        for provenance in self._all_provenance():
            if provenance.producer not in self.producer_names:
                raise ValueError(f"undeclared producer {provenance.producer!r}")

    def _all_provenance(self) -> Iterator[Provenance]:
        doc = self.doc
        for node in doc.nodes:
            yield node.provenance
            yield from (e.provenance for e in node.evidence)
        for edge in doc.edges:
            yield from (e.provenance for e in edge.evidence)
        for classification in doc.classifications:
            yield from (e.provenance for e in classification.evidence)
        yield from (r.provenance for r in doc.unresolved_references)
        yield from (i.provenance for i in doc.extraction_issues)

    # -- helpers -------------------------------------------------------------

    def _evidence_refs(self, evidence: Iterable[object], label: str) -> None:
        for item in evidence:
            if isinstance(item, SpanEvidence | ConfigEvidence):
                self._file(item.file_key, f"{label} evidence")
            elif isinstance(item, DerivedEvidence):
                for key in item.node_keys:
                    self._node(key, f"{label} derived evidence")
                for ref in item.edge_refs:
                    if (ref.kind, ref.source_key, ref.target_key) not in self.doc.edges_by_ref:
                        raise ValueError(f"{label}: derived evidence cites a missing edge")

    def _node(self, key: str, context: str) -> Node:
        try:
            return self.nodes[key]
        except KeyError:
            raise ValueError(f"{context}: unknown node {key!r}") from None

    def _file(self, key: str, context: str) -> Node:
        node = self._node(key, context)
        if node.kind is not NodeKind.FILE:
            raise ValueError(f"{context}: {key!r} is a {node.kind}, not a file")
        return node


def endpoint_scope(file_key: str, language: str | None, packages: Iterable[Node]) -> str:
    """The scope of an endpoint registered in ``file_key`` (CIM v1, section 4).

    The nearest package whose root is an ancestor of the file; when several share
    that root, the ones whose ecosystem serves the file's language; when that still
    leaves several (or none), all of them. Among the remaining candidates the
    smallest key in code point order wins. Without any enclosing package, ``/``.
    """
    by_root: dict[str, list[Node]] = {}
    for package in packages:
        assert isinstance(package.attributes, PackageAttributes)
        by_root.setdefault(package.attributes.root_key, []).append(package)
    current = file_key
    while True:
        current = parent_path_key(current)
        if current in by_root:
            candidates = by_root[current]
            matching = [p for p in candidates if _serves(p, language)]
            return min(p.key for p in (matching or candidates))
        if current == REPOSITORY_KEY:
            return REPOSITORY_KEY


def _serves(package: Node, language: str | None) -> bool:
    assert isinstance(package.attributes, PackageAttributes)
    return language in ECOSYSTEM_LANGUAGES.get(package.attributes.ecosystem, frozenset())


def _no_duplicates(values: Iterable[object], what: str) -> None:
    counts = Counter(values)
    duplicates = [v for v, n in counts.items() if n > 1]
    if duplicates:
        raise ValueError(f"duplicate {what}: {duplicates[0]!r}")
