"""Ingest a public GitHub repository at one commit as an immutable, persisted snapshot.

    validate (github) -> resolve the commit (git) -> stop if already stored
    -> retrieve (git) -> analyze (analyzer) -> persist_snapshot (persistence)

Requires ``psycopg`` (``pip install -e .[postgres]``), so it is not imported by
``xraymyrepo.ingestion``; import ``xraymyrepo.ingestion.service``.

**Identity.** A snapshot is ``(github.com/<owner>/<name>, commit SHA, ANALYZER_VERSION,
config hash of the default AnalysisOptions)``, with owner and name lower-cased by
``parse_github_url``. The same commit and analyzer configuration always give the same
identity, and a stored snapshot is never re-analyzed or replaced: ingesting it again
returns it. A ref is resolved to a commit first and only the commit identifies the
snapshot.

**Representation.** Retrieval does not write symlinks or submodules, so the analyzer
never sees them and the snapshot holds no node or issue for them. The CIM document does
not record the omission; ``IngestResult.skipped`` does, for the ingestion that
retrieved the commit. See docs/ingestion/ingestion-v1.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xraymyrepo.analyzer import ANALYZER_VERSION, AnalysisOptions, AnalyzerContractError, analyze
from xraymyrepo.cim import RepositoryRef, SnapshotStatus
from xraymyrepo.cim.snapshot import SnapshotIdentity
from xraymyrepo.persistence import (
    RepositoryIdentityError,
    SnapshotExistsError,
    find_snapshot,
    persist_snapshot,
)
from xraymyrepo.persistence.queries import get_repository
from xraymyrepo.persistence.snapshots import Connection

from .git import RetrievalLimits, SkippedEntry, deadline, resolve_commit, retrieve
from .github import IngestionTarget, ingestion_target


@dataclass(frozen=True)
class IngestResult:
    snapshot_id: int
    repository: RepositoryRef
    commit_sha: str
    extractor_version: str
    config_hash: str
    already_existed: bool
    """True when a complete snapshot with this identity was already stored, by an
    earlier ingestion or by a concurrent one that persisted first."""
    skipped: tuple[SkippedEntry, ...] | None
    """Tree entries the snapshot does not represent (symlinks and submodules), in tree
    order, when this call retrieved the commit. ``None`` when it did not, because the
    snapshot already existed: the omissions are then unknown here, not absent."""

    @property
    def identity(self) -> SnapshotIdentity:
        return SnapshotIdentity(repository=self.repository, commit_sha=self.commit_sha,
                                extractor_version=self.extractor_version,
                                config_hash=self.config_hash)  # fmt: skip


def ingest(
    conn: Connection,
    url: str,
    *,
    commit_sha: str | None = None,
    ref: str | None = None,
    limits: RetrievalLimits | None = None,
    temp_dir: Path | None = None,
) -> IngestResult:
    """Ingest the repository at ``url`` at exactly one of ``commit_sha`` or ``ref``.

    Use an autocommit connection (as persistence does): retrieval can take minutes and
    no transaction is held open meanwhile; the snapshot is stored in one transaction.
    The temporary checkout is removed before persisting, and on every failure. One
    ``limits.deadline`` covers resolving the ref and the whole retrieval (fetch, tree
    inspection, materialization); analysis and persistence are not timed.

    Raises ``IngestionInputError`` for invalid input (before any database or network
    access); ``RepositoryIdentityError`` when the repository is stored with other letter
    case (before retrieval); the ``RetrievalError`` subclasses for retrieval; analyzer
    errors as the analyzer raises them; ``SnapshotExistsError`` when the identity is
    stored but not complete; and database errors unchanged.
    """
    target = ingestion_target(url, commit_sha=commit_sha, ref=ref)
    options = AnalysisOptions()
    _check_stored_spelling(conn, target.repository)
    ends_at = deadline(limits)  # one limits.deadline for resolving and retrieving together
    sha = resolve_commit(target, limits=limits, temp_dir=temp_dir, ends_at=ends_at)
    identity = SnapshotIdentity(
        repository=target.repository,
        commit_sha=sha,
        extractor_version=ANALYZER_VERSION,
        config_hash=options.config().config_hash(),
    )
    stored = find_snapshot(conn, identity)
    if stored is not None:
        return _existing(identity, stored.id, stored.status, skipped=None)

    pinned = IngestionTarget(target.repository, commit_sha=sha)
    with retrieve(pinned, limits=limits, temp_dir=temp_dir, ends_at=ends_at) as tree:
        document = analyze(tree.root, repository=target.repository, commit_sha=sha,
                           options=options)  # fmt: skip
        skipped = tree.skipped
    if document.snapshot != identity:
        raise AnalyzerContractError(
            f"analyzed snapshot {document.snapshot.as_tuple()} does not have the expected "
            f"identity {identity.as_tuple()}"
        )
    try:
        snapshot_id = persist_snapshot(conn, document)
    except SnapshotExistsError as exc:  # a concurrent ingestion persisted it first
        return _existing(identity, exc.snapshot_id, exc.status, skipped=skipped)
    return _result(identity, snapshot_id, already_existed=False, skipped=skipped)


def _check_stored_spelling(conn: Connection, repository: RepositoryRef) -> None:
    """Fail before any retrieval when persistence would reject the repository's spelling."""
    record = get_repository(conn, repository.host, repository.owner, repository.name)
    if record is not None and record.repository != repository:
        stored = record.repository
        raise RepositoryIdentityError(
            f"repository {repository.slug!r} is stored as "
            f"{stored.host}/{stored.owner}/{stored.name}"
        )


def _existing(
    identity: SnapshotIdentity,
    snapshot_id: int,
    status: SnapshotStatus,
    *,
    skipped: tuple[SkippedEntry, ...] | None,
) -> IngestResult:
    """A stored snapshot counts as this ingestion's result only when it is complete."""
    if status is not SnapshotStatus.COMPLETE:
        raise SnapshotExistsError(snapshot_id, status)
    return _result(identity, snapshot_id, already_existed=True, skipped=skipped)


def _result(
    identity: SnapshotIdentity,
    snapshot_id: int,
    *,
    already_existed: bool,
    skipped: tuple[SkippedEntry, ...] | None,
) -> IngestResult:
    return IngestResult(snapshot_id, identity.repository, identity.commit_sha,
                        identity.extractor_version, identity.config_hash, already_existed,
                        skipped)  # fmt: skip
