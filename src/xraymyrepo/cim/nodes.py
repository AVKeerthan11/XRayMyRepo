"""Snapshot-tier nodes.

Containment is ``parent_key``: the *syntactic declaration tree*
repository -> directory -> file -> type/function (nested). It is not stored as an
edge. Packages and external packages are not in the tree (``parent_key`` is None);
endpoints hang off the file where they are registered and are keyed by the
nearest enclosing package (``EndpointAttributes.scope_key``). Groups are not snapshot
nodes at all; see ``derivation.py``.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, SerializeAsAny, field_validator, model_validator

from ._base import MAX_EVIDENCE_SAMPLE, CIMModel, GitOid, Sha256, Slug, Text
from .enums import (
    SNAPSHOT_BASES,
    SNAPSHOT_NODE_KINDS,
    SYMBOL_NODE_KINDS,
    Basis,
    Confidence,
    EndpointProtocol,
    ExtractionStatus,
    FunctionKind,
    NodeKind,
    TypeKind,
)
from .evidence import FactEvidence, SourceSpan, strongest_basis
from .keys import (
    REPOSITORY_KEY,
    InvalidKeyError,
    decode_path,
    endpoint_key,
    external_package_key,
    normalize_http_route,
    package_key,
    parse_endpoint_key,
    parse_symbol_key,
    validate_key,
)
from .provenance import Provenance

# ---------------------------------------------------------------------------
# Kind-specific attributes. Fields that are filtered on often are top-level Node
# fields (and real database columns); everything else lives here.


class NodeAttributes(CIMModel):
    pass


class RepositoryAttributes(NodeAttributes):
    pass


class DirectoryAttributes(NodeAttributes):
    pass


class FileAttributes(NodeAttributes):
    module_name: str | None = None
    """Importable dotted module name where the language has one (``app.users.service``).
    This is how "module" is represented: as an attribute of a file, not a node."""


class TypeAttributes(NodeAttributes):
    type_kind: TypeKind


class FunctionAttributes(NodeAttributes):
    function_kind: FunctionKind
    is_async: bool = False


class EndpointAttributes(NodeAttributes):
    protocol: EndpointProtocol
    method: str | None = None
    """HTTP method (upper case); only for ``http``."""
    route: Text
    """Canonical route or operation name: ``/users/{id}``, ``pkg.Svc/Method``, a topic."""
    raw_route: Text | None = None
    """The route as written, when it differs from ``route`` (``/orders/:id``)."""
    framework: Slug | None = None
    scope_key: str
    """Key of the nearest package enclosing the registration file, or ``/`` (the
    repository) when there is none. Part of the endpoint key, so the same route in
    two packages is two endpoints. Never the handler: endpoint identity is semantic."""


# Languages served by each package ecosystem. Used to pick an endpoint's scope when
# several packages share one root directory (see ``document.endpoint_scope``).
ECOSYSTEM_LANGUAGES: dict[str, frozenset[str]] = {
    "pypi": frozenset({"python"}),
    "npm": frozenset({"javascript", "typescript"}),
}


class PackageAttributes(NodeAttributes):
    ecosystem: Slug
    package_name: Text
    manifest_key: str
    """File key of the manifest that declares the package."""
    root_key: str
    """Directory key (or the repository key) where the package is rooted."""


class ExternalPackageAttributes(NodeAttributes):
    ecosystem: Slug
    package_name: Text


NODE_ATTRIBUTE_MODELS: dict[NodeKind, type[NodeAttributes]] = {
    NodeKind.REPOSITORY: RepositoryAttributes,
    NodeKind.DIRECTORY: DirectoryAttributes,
    NodeKind.FILE: FileAttributes,
    NodeKind.TYPE: TypeAttributes,
    NodeKind.FUNCTION: FunctionAttributes,
    NodeKind.ENDPOINT: EndpointAttributes,
    NodeKind.PACKAGE: PackageAttributes,
    NodeKind.EXTERNAL_PACKAGE: ExternalPackageAttributes,
}

# Allowed parent kinds in the declaration tree; None means "no parent".
PARENT_KINDS: dict[NodeKind, frozenset[NodeKind] | None] = {
    NodeKind.REPOSITORY: None,
    NodeKind.DIRECTORY: frozenset({NodeKind.REPOSITORY, NodeKind.DIRECTORY}),
    NodeKind.FILE: frozenset({NodeKind.REPOSITORY, NodeKind.DIRECTORY}),
    NodeKind.TYPE: frozenset({NodeKind.FILE, NodeKind.TYPE, NodeKind.FUNCTION}),
    NodeKind.FUNCTION: frozenset({NodeKind.FILE, NodeKind.TYPE, NodeKind.FUNCTION}),
    NodeKind.ENDPOINT: frozenset({NodeKind.FILE}),
    NodeKind.PACKAGE: None,
    NodeKind.EXTERNAL_PACKAGE: None,
}

_SPANNED_KINDS = SYMBOL_NODE_KINDS | {NodeKind.ENDPOINT}


class Node(CIMModel):
    key: str
    kind: NodeKind
    name: Text
    parent_key: str | None
    language: Slug | None = None
    span: SourceSpan | None = None
    basis: Basis
    confidence: Confidence
    provenance: Provenance
    evidence: tuple[FactEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE_SAMPLE)
    extraction_status: ExtractionStatus | None = None
    """Files only: what the extractors could see in this file."""
    blob_sha: GitOid | None = None
    """Files only: git blob id of the content, for fetching source and caching."""
    content_hash: Sha256 | None = None
    signature_hash: Sha256 | None = None
    attributes: SerializeAsAny[NodeAttributes]

    @model_validator(mode="before")
    @classmethod
    def _typed_attributes(cls, data: Any) -> Any:
        if isinstance(data, dict):
            try:
                kind = NodeKind(str(data.get("kind")))
            except ValueError:
                return data  # field validation reports the bad kind
            model = NODE_ATTRIBUTE_MODELS.get(kind)
            attrs = data.get("attributes", {})
            if model is not None and not isinstance(attrs, NodeAttributes):
                data = {**data, "attributes": model.model_validate(attrs)}
        return data

    @field_validator("kind")
    @classmethod
    def _snapshot_kind(cls, kind: NodeKind) -> NodeKind:
        if kind not in SNAPSHOT_NODE_KINDS:
            raise ValueError(f"'{kind}' nodes belong to the derivation tier, not a snapshot")
        return kind

    @field_validator("basis")
    @classmethod
    def _snapshot_basis(cls, basis: Basis) -> Basis:
        if basis not in SNAPSHOT_BASES:
            raise ValueError(f"snapshot nodes cannot have basis '{basis}'")
        return basis

    @model_validator(mode="after")
    def _consistent(self) -> Node:
        kind = self.kind
        try:
            validate_key(kind, self.key)
        except InvalidKeyError as exc:
            raise ValueError(str(exc)) from None
        expected_attrs = NODE_ATTRIBUTE_MODELS[kind]
        if type(self.attributes) is not expected_attrs:
            raise ValueError(f"{kind} nodes need {expected_attrs.__name__}")

        if (PARENT_KINDS[kind] is None) != (self.parent_key is None):
            raise ValueError(
                f"{kind} nodes {'must not' if PARENT_KINDS[kind] is None else 'must'} have a parent"
            )
        if (kind is NodeKind.FILE) != (self.extraction_status is not None):
            raise ValueError("extraction_status is required on files and only on files")
        if (kind is NodeKind.FILE) != (self.blob_sha is not None):
            raise ValueError("blob_sha is required on files and only on files")
        if kind not in SYMBOL_NODE_KINDS and (self.content_hash or self.signature_hash):
            raise ValueError("fingerprints apply only to type and function nodes")
        if (kind in _SPANNED_KINDS) != (self.span is not None):
            raise ValueError("span is required on type/function/endpoint nodes and only there")
        if self.basis is Basis.OBSERVED and self.confidence is not Confidence.HIGH:
            raise ValueError("observed claims always have high confidence")
        if self.basis in (Basis.HEURISTIC, Basis.INFERRED) and self.provenance.rule is None:
            raise ValueError(f"{self.basis} nodes must name the rule that produced them")
        if self.evidence and self.basis is not strongest_basis(self.evidence):
            raise ValueError("node basis must equal the strongest basis of its evidence")
        if not self.evidence and self.basis is not Basis.OBSERVED:
            raise ValueError(f"{self.basis} nodes need evidence")
        self._check_name()
        return self

    def _check_name(self) -> None:
        attrs = self.attributes
        expected: str | None = None
        if self.kind in (NodeKind.DIRECTORY, NodeKind.FILE):
            expected = decode_path(self.key).rsplit("/", 1)[-1]
        elif self.kind in SYMBOL_NODE_KINDS:
            expected = parse_symbol_key(self.key).name
        elif isinstance(attrs, EndpointAttributes):
            try:
                expected_key = endpoint_key(
                    attrs.protocol, attrs.route, attrs.method, scope=attrs.scope_key
                )
            except InvalidKeyError as exc:
                raise ValueError(str(exc)) from None
            if expected_key != self.key:
                raise ValueError(f"endpoint key {self.key!r} does not match its attributes")
            if attrs.protocol is EndpointProtocol.HTTP:
                if normalize_http_route(attrs.route) != attrs.route:
                    raise ValueError(f"HTTP route must be canonical: {attrs.route!r}")
                if attrs.raw_route and normalize_http_route(attrs.raw_route) != attrs.route:
                    raise ValueError("raw_route does not normalize to route")
            expected = parse_endpoint_key(self.key).name
        elif isinstance(attrs, PackageAttributes | ExternalPackageAttributes):
            builder = package_key if self.kind is NodeKind.PACKAGE else external_package_key
            if builder(attrs.ecosystem, attrs.package_name) != self.key:
                raise ValueError(f"package key {self.key!r} does not match its attributes")
            expected = attrs.package_name
        if expected is not None and self.name != expected:
            raise ValueError(f"{self.kind} name must be {expected!r}, got {self.name!r}")

    @property
    def is_root(self) -> bool:
        return self.key == REPOSITORY_KEY
