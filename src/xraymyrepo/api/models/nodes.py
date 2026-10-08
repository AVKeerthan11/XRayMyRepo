"""Nodes: tree items, details, search hits and diagnostics."""

from __future__ import annotations

from typing import Literal

from xraymyrepo.cim import (
    Basis,
    ClassificationFacet,
    Confidence,
    EdgeKind,
    ExtractionIssue,
    ExtractionStatus,
    SourceSpan,
    UnresolvedReference,
)
from xraymyrepo.cim.evidence import FactEvidence
from xraymyrepo.cim.nodes import (
    DirectoryAttributes,
    EndpointAttributes,
    ExternalPackageAttributes,
    FileAttributes,
    FunctionAttributes,
    PackageAttributes,
    RepositoryAttributes,
    TypeAttributes,
)
from xraymyrepo.cim.provenance import Provenance

from .common import ApiModel, NodeBrief, SnapshotNodeKind

NodeAttributes = (
    RepositoryAttributes
    | DirectoryAttributes
    | FileAttributes
    | TypeAttributes
    | FunctionAttributes
    | EndpointAttributes
    | PackageAttributes
    | ExternalPackageAttributes
)
"""The CIM attribute model of the node's kind."""


class Tag(ApiModel):
    """A classification reduced to facet and value, for badges and colouring."""

    facet: ClassificationFacet
    value: str


class NodeSummary(ApiModel):
    """A node as a tree, list or search item."""

    key: str
    kind: SnapshotNodeKind
    name: str
    parent_key: str | None
    language: str | None
    extraction_status: ExtractionStatus | None
    child_count: int
    """Children in the declaration tree; 0 for leaves (and for packages, which are not
    in the tree)."""
    tags: list[Tag]


class ClassificationDetail(ApiModel):
    facet: ClassificationFacet
    value: str
    basis: Basis
    confidence: Confidence
    contested: bool
    evidence: list[FactEvidence]


class EdgeCount(ApiModel):
    outgoing: int
    incoming: int


class DiagnosticCounts(ApiModel):
    unresolved_references: int
    extraction_issues: int


class NodeDetail(NodeSummary):
    span: SourceSpan | None
    basis: Basis
    confidence: Confidence
    provenance: Provenance
    blob_sha: str | None
    content_hash: str | None
    signature_hash: str | None
    attributes: NodeAttributes
    evidence: list[FactEvidence]
    classifications: list[ClassificationDetail]
    ancestors: list[NodeBrief]
    """Declaration-tree path from the root to the parent; empty outside the tree."""
    relationship_counts: dict[EdgeKind, EdgeCount]
    """Only kinds the node has edges of."""
    diagnostic_counts: DiagnosticCounts


class SearchHit(ApiModel):
    node: NodeSummary
    matched: Literal["name", "key"]


class Diagnostics(ApiModel):
    """What the extractors could not see around a node: unresolved references whose
    source or file is the node, and extraction issues of a file."""

    node: NodeBrief
    unresolved_references: list[UnresolvedReference]
    extraction_issues: list[ExtractionIssue]
