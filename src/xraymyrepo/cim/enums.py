"""Closed vocabularies of the CIM v1 contract.

Every enum here is part of the contract. Adding a value is a contract change that
must be mirrored in the PostgreSQL CHECK constraints (enforced by
``tests/test_schema_consistency.py``) and in ``docs/cim/CIM-v1.md``.
"""

from __future__ import annotations

from enum import StrEnum


class NodeKind(StrEnum):
    REPOSITORY = "repository"
    DIRECTORY = "directory"
    FILE = "file"
    TYPE = "type"
    FUNCTION = "function"
    ENDPOINT = "endpoint"
    PACKAGE = "package"
    EXTERNAL_PACKAGE = "external_package"
    # Groups belong to the derivation tier (a lens projection), never to a snapshot.
    GROUP = "group"


SNAPSHOT_NODE_KINDS: frozenset[NodeKind] = frozenset(NodeKind) - {NodeKind.GROUP}
SYMBOL_NODE_KINDS: frozenset[NodeKind] = frozenset({NodeKind.TYPE, NodeKind.FUNCTION})
PATH_NODE_KINDS: frozenset[NodeKind] = frozenset({NodeKind.DIRECTORY, NodeKind.FILE})


class TypeKind(StrEnum):
    CLASS = "class"
    INTERFACE = "interface"
    STRUCT = "struct"
    ENUM = "enum"
    TRAIT = "trait"
    PROTOCOL = "protocol"
    TYPE_ALIAS = "type_alias"


class FunctionKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    ACCESSOR = "accessor"


class EndpointProtocol(StrEnum):
    HTTP = "http"
    GRPC = "grpc"
    GRAPHQL = "graphql"
    WEBSOCKET = "websocket"
    CLI = "cli"
    MESSAGE = "message"


class EdgeKind(StrEnum):
    """Stored relationship kinds. Direction is always ``source -> target``."""

    IMPORTS = "IMPORTS"
    REQUIRES = "REQUIRES"
    INHERITS = "INHERITS"
    HANDLES = "HANDLES"
    TESTS = "TESTS"
    CALLS = "CALLS"


class Basis(StrEnum):
    """How a claim is known. Independent of ``Confidence``."""

    OBSERVED = "observed"
    RESOLVED = "resolved"
    HEURISTIC = "heuristic"
    INFERRED = "inferred"
    INTERPRETED = "interpreted"

    @property
    def strength(self) -> int:
        """Higher is more directly grounded in source. Used to pick a record's basis."""
        return _BASIS_STRENGTH[self]


_BASIS_STRENGTH = {
    Basis.OBSERVED: 5,
    Basis.RESOLVED: 4,
    Basis.HEURISTIC: 3,
    Basis.INFERRED: 2,
    Basis.INTERPRETED: 1,
}

# Snapshot facts are reproducible from (commit, extractor version, config).
SNAPSHOT_BASES: frozenset[Basis] = frozenset({Basis.OBSERVED, Basis.RESOLVED, Basis.HEURISTIC})
# Derivations add algorithmic inference on top of facts. AI is never a basis here.
DERIVATION_BASES: frozenset[Basis] = SNAPSHOT_BASES | {Basis.INFERRED}


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SnapshotStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class ExtractionStatus(StrEnum):
    """Per-file coverage. Absence of a relationship only means something for ``full``."""

    FULL = "full"  # every extractor applicable to the file's language ran without error
    PARTIAL = "partial"  # parsed, but with recovered errors or a failed extractor
    FAILED = "failed"  # could not be parsed; nothing was extracted
    UNSUPPORTED = "unsupported"  # no extractor supports the language: a coverage gap
    EXCLUDED = "excluded"  # deliberately not extracted by the snapshot's exclusion policy


EXTRACTED_STATUSES: frozenset[ExtractionStatus] = frozenset(
    {ExtractionStatus.FULL, ExtractionStatus.PARTIAL}
)


class ReferenceKind(StrEnum):
    """Kinds of source references that may fail to resolve."""

    IMPORT = "import"
    CALL = "call"
    INHERITANCE = "inheritance"
    HANDLER = "handler"


class UnresolvedReason(StrEnum):
    NOT_FOUND = "not_found"
    DYNAMIC = "dynamic"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED_LANGUAGE = "unsupported_language"


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ClassificationFacet(StrEnum):
    ROLE = "role"
    ORIGIN = "origin"
    LAYER = "layer"


class Role(StrEnum):
    TEST_FILE = "test_file"
    TEST_FUNCTION = "test_function"
    ORM_ENTITY = "orm_entity"
    SCHEMA = "schema"
    CONFIG = "config"
    ENTRYPOINT = "entrypoint"
    MANIFEST = "manifest"


class Origin(StrEnum):
    SOURCE = "source"
    GENERATED = "generated"
    VENDORED = "vendored"
    DOCS = "docs"
    BUILD_ARTIFACT = "build_artifact"


class ExclusionAction(StrEnum):
    """What the extractor does with files of a given origin."""

    EXTRACT = "extract"  # extract symbols and relationships
    FILE_ONLY = "file_only"  # record the file node only (extraction_status = excluded)


class RequirementScope(StrEnum):
    PROD = "prod"
    DEV = "dev"
    OPTIONAL = "optional"
    PEER = "peer"


class InheritanceMode(StrEnum):
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    MIXIN = "mixin"


class CallKind(StrEnum):
    DIRECT = "direct"
    METHOD = "method"
    CONSTRUCTOR = "constructor"


class LensKind(StrEnum):
    DIRECTORY = "directory"
    PACKAGE = "package"
    INFERRED = "inferred"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MatchMethod(StrEnum):
    """How a node in one snapshot was matched to a node in another."""

    SAME_KEY = "same_key"
    GIT_RENAME = "git_rename"
    SAME_CONTENT_HASH = "same_content_hash"


class AnnotationKind(StrEnum):
    LABEL = "label"
    SUMMARY = "summary"
    EXPLANATION = "explanation"
