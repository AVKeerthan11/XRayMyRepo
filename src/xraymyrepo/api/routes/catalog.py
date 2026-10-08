"""Repository and snapshot discovery."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import TypeAdapter

from xraymyrepo.cim import SnapshotStatus
from xraymyrepo.persistence import queries

from .. import cursors, mapping
from ..db import Conn
from ..deps import DEFAULT_LIMIT, CompleteSnapshot, Cursor, Limit
from ..errors import not_found, responses
from ..models import Page, Repository, SnapshotDetail, SnapshotSummary

router = APIRouter(tags=["repositories and snapshots"])

_REPOSITORY_CURSOR: TypeAdapter[queries.RepositorySortKey] = TypeAdapter(queries.RepositorySortKey)
_SNAPSHOT_CURSOR: TypeAdapter[int] = TypeAdapter(int)


@router.get("/repositories", summary="List repositories", responses=responses(400))
def list_repositories(
    conn: Conn, limit: Limit = DEFAULT_LIMIT, cursor: Cursor = None
) -> Page[Repository]:
    """Ordered by host, then owner and name (ignoring case)."""
    after = cursors.decode(cursor, _REPOSITORY_CURSOR)
    rows = queries.list_repositories(conn, after=after, limit=limit + 1)
    items, next_cursor = cursors.page(
        rows, limit, lambda r: queries.repository_sort_key(r.repository)
    )
    return Page(items=[mapping.repository(r) for r in items], next_cursor=next_cursor)


def _repository(conn: Conn, host: str, owner: str, name: str) -> queries.RepositoryRecord:
    record = queries.get_repository(conn, host, owner, name)
    if record is None:
        raise not_found("repository_not_found", f"Repository {host}/{owner}/{name} is unknown.")
    return record


@router.get(
    "/repositories/{host}/{owner}/{name}",
    summary="One repository (owner and name match ignoring case)",
    responses=responses(404),
)
def get_repository(conn: Conn, host: str, owner: str, name: str) -> Repository:
    return mapping.repository(_repository(conn, host, owner, name))


@router.get(
    "/repositories/{host}/{owner}/{name}/snapshots",
    summary="List or find a repository's snapshots, newest first",
    responses=responses(400, 404),
)
def list_snapshots(
    conn: Conn,
    host: str,
    owner: str,
    name: str,
    commit_sha: str | None = None,
    extractor_version: str | None = None,
    config_hash: str | None = None,
    status: Annotated[SnapshotStatus, Query()] = SnapshotStatus.COMPLETE,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[SnapshotSummary]:
    """Filter by any part of the snapshot identity to find one snapshot."""
    repository = _repository(conn, host, owner, name).repository
    rows = queries.list_snapshots(
        conn, repository, commit_sha=commit_sha, extractor_version=extractor_version,
        config_hash=config_hash, status=status,
        after=cursors.decode(cursor, _SNAPSHOT_CURSOR), limit=limit + 1,
    )  # fmt: skip
    items, next_cursor = cursors.page(rows, limit, lambda s: s.id)
    return Page(items=[mapping.snapshot_summary(s) for s in items], next_cursor=next_cursor)


@router.get(
    "/snapshots/{snapshot_id}",
    summary="Snapshot metadata, configuration, root node and statistics",
    responses=responses(404, 409),
)
def get_snapshot(conn: Conn, snapshot: CompleteSnapshot) -> SnapshotDetail:
    setup = queries.get_snapshot_setup(conn, snapshot.id)
    root = queries.get_node(conn, snapshot.id, "/")
    assert root is not None  # every snapshot has exactly one repository node
    return SnapshotDetail(
        **dict(mapping.snapshot_summary(snapshot)),
        config=setup.config,
        producers=list(setup.producers),
        coverage=list(setup.coverage),
        root=mapping.summaries(conn, snapshot.id, [root])[0],
        stats=mapping.snapshot_stats(queries.snapshot_stats(conn, snapshot.id)),
    )
