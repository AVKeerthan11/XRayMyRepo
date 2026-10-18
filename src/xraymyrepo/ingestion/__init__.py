"""Repository ingestion: a public GitHub repository at one commit -> a persisted snapshot.

``github`` validates ingestion inputs (pure); ``git`` retrieves one commit into a
temporary directory under resource limits; ``service.ingest`` analyzes and persists it
(import it from ``xraymyrepo.ingestion.service``: it needs ``psycopg``, the rest does
not). This package sits above the analyzer and persistence and is never imported by
them, by the derivation packages or by the API. See docs/ingestion/ingestion-v1.md.
"""

from .git import (
    CommitNotFoundError,
    GitUnavailableError,
    RefResolutionError,
    RemoteUnavailableError,
    RetrievalError,
    RetrievalLimitError,
    RetrievalLimits,
    RetrievalTimeoutError,
    RetrievedTree,
    SkippedEntry,
    UnsafeTreeError,
    deadline,
    resolve_commit,
    retrieve,
)
from .github import (
    GITHUB_HOST,
    IngestionInputError,
    IngestionTarget,
    ingestion_target,
    parse_github_url,
    validate_commit_sha,
    validate_ref,
)

__all__ = [
    "GITHUB_HOST",
    "CommitNotFoundError",
    "GitUnavailableError",
    "IngestionInputError",
    "IngestionTarget",
    "RefResolutionError",
    "RemoteUnavailableError",
    "RetrievalError",
    "RetrievalLimitError",
    "RetrievalLimits",
    "RetrievalTimeoutError",
    "RetrievedTree",
    "SkippedEntry",
    "UnsafeTreeError",
    "deadline",
    "ingestion_target",
    "parse_github_url",
    "resolve_commit",
    "retrieve",
    "validate_commit_sha",
    "validate_ref",
]
