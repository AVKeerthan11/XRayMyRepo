"""Read queries over stored snapshots, for callers that need part of a snapshot.

``load_snapshot`` materializes a whole document; these functions answer focused
questions (one node, one page of children, the edges of one node) with a bounded
number of indexed queries. They identify nodes by CIM key, never by database id,
and return CIM records or small frozen records.

Every function that takes a ``snapshot_id`` assumes the caller has checked that
the snapshot is complete (``get_snapshot``); the data of a pending or failed
snapshot is not meant to be read. Pagination is keyset-based: each paged function
takes ``after`` (the sort key of the last item already returned, see the
``*_sort_key`` helpers) and returns at most ``limit`` items in that order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

import psycopg
from pydantic import TypeAdapter

from xraymyrepo.cim import (
    Classification,
    ClassificationFacet,
    CoverageDeclaration,
    Edge,
    EdgeKind,
    EdgeRef,
    ExtractionIssue,
    IssueSeverity,
    NodeKind,
    RepositoryRef,
    SnapshotStatus,
    UnresolvedReason,
    UnresolvedReference,
)
from xraymyrepo.cim.nodes import Node
from xraymyrepo.cim.provenance import Producer
from xraymyrepo.cim.snapshot import AnalysisConfig, SnapshotIdentity

from . import _rows

Connection = psycopg.Connection[Any]

REQUIRED_MIGRATIONS: tuple[str, ...] = (
    "0001_cim_v1_snapshot_tier",
    "0002_snapshot_children_immutable",
)
"""Migrations this code depends on, in order."""

_PRODUCERS: TypeAdapter[tuple[Producer, ...]] = TypeAdapter(tuple[Producer, ...])
_COVERAGE: TypeAdapter[tuple[CoverageDeclaration, ...]] = TypeAdapter(
    tuple[CoverageDeclaration, ...]
)


# ---------------------------------------------------------------------------
# Records


@dataclass(frozen=True)
class SnapshotRecord:
    id: int
    identity: SnapshotIdentity
    cim_schema_version: str
    status: SnapshotStatus
    failure_reason: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class SnapshotSetup:
    """What a snapshot was extracted with, in canonical order (as ``load_snapshot``)."""

    config: AnalysisConfig
    producers: tuple[Producer, ...]
    coverage: tuple[CoverageDeclaration, ...]


@dataclass(frozen=True)
class SnapshotStats:
    nodes_by_kind: dict[NodeKind, int]
    edges_by_kind: dict[EdgeKind, int]
    unresolved_by_reason: dict[UnresolvedReason, int]
    issues_by_severity: dict[IssueSeverity, int]


@dataclass(frozen=True)
class RepositoryRecord:
    repository: RepositoryRef
    snapshot_count: int
    """Complete snapshots."""
    latest_snapshot: SnapshotRecord | None
    """The most recently created complete snapshot. Not "the newest commit": commit
    dates and branches are not stored."""


@dataclass(frozen=True)
class NodeBrief:
    key: str
    kind: NodeKind
    name: str


class Direction(StrEnum):
    """Which end of an edge the focus node is. Edges keep their stored direction."""

    OUT = "out"
    IN = "in"


@dataclass(frozen=True)
class NodeEdge:
    edge: Edge
    direction: Direction
    peer: NodeBrief


@dataclass(frozen=True)
class SearchMatch:
    node: Node
    rank: int
    """0 exact name, 1 name prefix, 2 name substring, 3 key (path) substring."""

    @property
    def matched(self) -> Literal["name", "key"]:
        return "key" if self.rank == 3 else "name"


@dataclass(frozen=True)
class Diagnostics:
    unresolved_references: tuple[UnresolvedReference, ...]
    extraction_issues: tuple[ExtractionIssue, ...]


# ---------------------------------------------------------------------------
# Sort keys (the ``after`` values of the paged functions)

RepositorySortKey = tuple[str, str, str]
ChildSortKey = tuple[int, int, int, str]
SearchSortKey = tuple[int, str]
EdgeSortKey = tuple[int, str, str]

_CHILD_RANK = {NodeKind.DIRECTORY: 0, NodeKind.FILE: 1, NodeKind.ENDPOINT: 3}
# Directories, then files (by key); inside a file, symbols in source order, then
# endpoints. Must order exactly like child_sort_key.
_CHILD_ORDER = (
    "CASE n.kind WHEN 'directory' THEN 0 WHEN 'file' THEN 1 WHEN 'endpoint' THEN 3 ELSE 2 END, "
    "coalesce(n.start_line, 0), coalesce(n.start_col, 0), n.key"
)


def repository_sort_key(repository: RepositoryRef) -> RepositorySortKey:
    return (repository.host, repository.owner.lower(), repository.name.lower())


def child_sort_key(node: Node) -> ChildSortKey:
    span = node.span
    return (
        _CHILD_RANK.get(node.kind, 2),
        span.start_line if span else 0,
        span.start_col if span else 0,
        node.key,
    )


def search_sort_key(match: SearchMatch) -> SearchSortKey:
    return (match.rank, match.node.key)


def edge_sort_key(item: NodeEdge) -> EdgeSortKey:
    return (0 if item.direction is Direction.OUT else 1, item.edge.kind.value, item.peer.key)


# ---------------------------------------------------------------------------
# Service


def applied_migrations(conn: Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT version FROM schema_migration ORDER BY version")]


# ---------------------------------------------------------------------------
# Repositories and snapshots

_SNAPSHOT_COLUMNS = (
    "s.id, s.commit_sha, s.extractor_version, s.config_hash, s.cim_schema_version, s.status, "
    "s.failure_reason, s.created_at, s.finished_at"
)
_REPOSITORY_SELECT = (
    "SELECT r.host, r.owner, r.name, "
    "(SELECT count(*) FROM snapshot c WHERE c.repository_id = r.id AND c.status = 'complete'), "
    f"{_SNAPSHOT_COLUMNS.replace('s.', 'latest.')} "
    "FROM repository r LEFT JOIN LATERAL ("
    "SELECT * FROM snapshot s WHERE s.repository_id = r.id AND s.status = 'complete' "
    "ORDER BY s.id DESC LIMIT 1) latest ON true"
)
_REPOSITORY_ORDER = "r.host, lower(r.owner), lower(r.name)"


def _snapshot(repository: RepositoryRef, row: Sequence[Any]) -> SnapshotRecord:
    (id_, commit_sha, extractor_version, config_hash, schema, status, failure_reason, created_at,
     finished_at) = row  # fmt: skip
    return SnapshotRecord(
        id=int(id_),
        identity=SnapshotIdentity(
            repository=repository,
            commit_sha=commit_sha,
            extractor_version=extractor_version,
            config_hash=config_hash,
        ),
        cim_schema_version=schema,
        status=SnapshotStatus(status),
        failure_reason=failure_reason,
        created_at=created_at,
        finished_at=finished_at,
    )


def _repository(row: Sequence[Any]) -> RepositoryRecord:
    repository = RepositoryRef(host=row[0], owner=row[1], name=row[2])
    latest = None if row[4] is None else _snapshot(repository, row[4:])
    return RepositoryRecord(repository, int(row[3]), latest)


def list_repositories(
    conn: Connection, *, after: RepositorySortKey | None = None, limit: int
) -> list[RepositoryRecord]:
    """Repositories ordered by host, then owner and name ignoring case."""
    where = ""
    params: tuple[str, ...] = ()
    if after is not None:
        where = f" WHERE ({_REPOSITORY_ORDER}) > (%s, %s, %s)"
        params = after
    rows = conn.execute(
        f"{_REPOSITORY_SELECT}{where} ORDER BY {_REPOSITORY_ORDER} LIMIT %s", (*params, limit)
    ).fetchall()
    return [_repository(r) for r in rows]


def get_repository(conn: Connection, host: str, owner: str, name: str) -> RepositoryRecord | None:
    """Owner and name match ignoring case, like the repository identity index."""
    row = conn.execute(
        f"{_REPOSITORY_SELECT} WHERE r.host = %s AND lower(r.owner) = lower(%s) "
        "AND lower(r.name) = lower(%s)",
        (host, owner, name),
    ).fetchone()
    return None if row is None else _repository(row)


def list_snapshots(
    conn: Connection,
    repository: RepositoryRef,
    *,
    commit_sha: str | None = None,
    extractor_version: str | None = None,
    config_hash: str | None = None,
    status: SnapshotStatus | None = None,
    after: int | None = None,
    limit: int,
) -> list[SnapshotRecord]:
    """Snapshots of one repository, newest first (by creation)."""
    conditions = ["r.host = %s", "lower(r.owner) = lower(%s)", "lower(r.name) = lower(%s)"]
    params: list[Any] = [repository.host, repository.owner, repository.name]
    for column, value in (("s.commit_sha", commit_sha),
                          ("s.extractor_version", extractor_version),
                          ("s.config_hash", config_hash),
                          ("s.status", None if status is None else status.value)):  # fmt: skip
        if value is not None:
            conditions.append(f"{column} = %s")
            params.append(value)
    if after is not None:
        conditions.append("s.id < %s")
        params.append(after)
    rows = conn.execute(
        f"SELECT r.host, r.owner, r.name, {_SNAPSHOT_COLUMNS} FROM snapshot s "
        f"JOIN repository r ON r.id = s.repository_id WHERE {' AND '.join(conditions)} "
        "ORDER BY s.id DESC LIMIT %s",
        (*params, limit),
    ).fetchall()
    return [_snapshot(RepositoryRef(host=r[0], owner=r[1], name=r[2]), r[3:]) for r in rows]


def get_snapshot(conn: Connection, snapshot_id: int) -> SnapshotRecord | None:
    row = conn.execute(
        f"SELECT r.host, r.owner, r.name, {_SNAPSHOT_COLUMNS} FROM snapshot s "
        "JOIN repository r ON r.id = s.repository_id WHERE s.id = %s",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return _snapshot(RepositoryRef(host=row[0], owner=row[1], name=row[2]), row[3:])


def get_snapshot_setup(conn: Connection, snapshot_id: int) -> SnapshotSetup:
    row = conn.execute(
        "SELECT s.config, s.coverage, "
        "(SELECT coalesce(jsonb_agg(jsonb_build_object('name', p.name, 'version', p.version, "
        "'config_hash', p.config_hash) ORDER BY p.name), '[]') "
        "FROM snapshot_producer sp JOIN producer p ON p.id = sp.producer_id "
        "WHERE sp.snapshot_id = s.id) FROM snapshot s WHERE s.id = %s",
        (snapshot_id,),
    ).fetchone()
    assert row is not None, f"snapshot {snapshot_id} does not exist"
    return SnapshotSetup(
        config=AnalysisConfig.model_validate(row[0]),
        producers=_PRODUCERS.validate_python(row[2]),
        coverage=tuple(sorted(_COVERAGE.validate_python(row[1]), key=lambda c: c.language)),
    )


def snapshot_stats(conn: Connection, snapshot_id: int) -> SnapshotStats:
    rows = conn.execute(
        "SELECT 'node', kind, count(*) FROM node WHERE snapshot_id = %(s)s GROUP BY kind "
        "UNION ALL SELECT 'edge', kind, count(*) FROM edge WHERE snapshot_id = %(s)s "
        "GROUP BY kind "
        "UNION ALL SELECT 'unresolved', reason, count(*) FROM unresolved_reference "
        "WHERE snapshot_id = %(s)s GROUP BY reason "
        "UNION ALL SELECT 'issue', severity, count(*) FROM extraction_issue "
        "WHERE snapshot_id = %(s)s GROUP BY severity",
        {"s": snapshot_id},
    ).fetchall()
    counts: dict[str, dict[str, int]] = {"node": {}, "edge": {}, "unresolved": {}, "issue": {}}
    for table, value, count in rows:
        counts[table][value] = int(count)
    return SnapshotStats(
        nodes_by_kind={NodeKind(k): v for k, v in sorted(counts["node"].items())},
        edges_by_kind={EdgeKind(k): v for k, v in sorted(counts["edge"].items())},
        unresolved_by_reason={
            UnresolvedReason(k): v for k, v in sorted(counts["unresolved"].items())
        },
        issues_by_severity={IssueSeverity(k): v for k, v in sorted(counts["issue"].items())},
    )


# ---------------------------------------------------------------------------
# Nodes

_NODE_ID = "(SELECT id FROM node WHERE snapshot_id = %(s)s AND key = %(key)s)"


def get_node(conn: Connection, snapshot_id: int, key: str) -> Node | None:
    row = conn.execute(
        f"SELECT {_rows.NODE_COLUMNS}{_rows.NODE_FROM} WHERE n.snapshot_id = %s AND n.key = %s",
        (snapshot_id, key),
    ).fetchone()
    return None if row is None else _rows.node(row)


def get_node_brief(conn: Connection, snapshot_id: int, key: str) -> NodeBrief | None:
    row = conn.execute(
        "SELECT key, kind, name FROM node WHERE snapshot_id = %s AND key = %s", (snapshot_id, key)
    ).fetchone()
    return None if row is None else NodeBrief(row[0], NodeKind(row[1]), row[2])


def get_children(
    conn: Connection,
    snapshot_id: int,
    parent_key: str,
    *,
    kinds: Sequence[NodeKind] = (),
    after: ChildSortKey | None = None,
    limit: int,
) -> list[Node]:
    """The children of ``parent_key`` in the declaration tree, in ``child_sort_key`` order.

    Packages and external packages have no parent and are never anyone's children.
    """
    sql = (f"SELECT {_rows.NODE_COLUMNS}{_rows.NODE_FROM} "
           f"WHERE n.snapshot_id = %(s)s AND n.parent_id = {_NODE_ID}")  # fmt: skip
    params: dict[str, Any] = {"s": snapshot_id, "key": parent_key, "limit": limit}
    if kinds:
        sql += " AND n.kind = ANY(%(kinds)s)"
        params["kinds"] = [k.value for k in kinds]
    if after is not None:
        sql += f" AND ({_CHILD_ORDER}) > (%(r)s, %(line)s, %(col)s, %(after)s)"
        params.update(r=after[0], line=after[1], col=after[2], after=after[3])
    rows = conn.execute(f"{sql} ORDER BY {_CHILD_ORDER} LIMIT %(limit)s", params).fetchall()
    return [_rows.node(r) for r in rows]


def count_children(conn: Connection, snapshot_id: int, keys: Sequence[str]) -> dict[str, int]:
    """Child counts for several nodes in one query (0 for leaves; unknown keys omitted)."""
    rows = conn.execute(
        "SELECT p.key, count(c.id) FROM node p "
        "LEFT JOIN node c ON c.snapshot_id = p.snapshot_id AND c.parent_id = p.id "
        "WHERE p.snapshot_id = %s AND p.key = ANY(%s) GROUP BY p.key",
        (snapshot_id, list(keys)),
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def get_ancestors(conn: Connection, snapshot_id: int, key: str) -> list[NodeBrief]:
    """The declaration-tree path from the root down to the node's parent."""
    rows = conn.execute(
        "WITH RECURSIVE up AS ("
        " SELECT p.id, p.parent_id, p.key, p.kind, p.name, p.depth FROM node n"
        " JOIN node p ON p.snapshot_id = n.snapshot_id AND p.id = n.parent_id"
        " WHERE n.snapshot_id = %(s)s AND n.key = %(key)s"
        " UNION ALL"
        " SELECT p.id, p.parent_id, p.key, p.kind, p.name, p.depth FROM up"
        " JOIN node p ON p.snapshot_id = %(s)s AND p.id = up.parent_id"
        ") SELECT key, kind, name FROM up ORDER BY depth",
        {"s": snapshot_id, "key": key},
    ).fetchall()
    return [NodeBrief(r[0], NodeKind(r[1]), r[2]) for r in rows]


def list_nodes(
    conn: Connection,
    snapshot_id: int,
    *,
    kinds: Sequence[NodeKind] = (),
    prefix: str | None = None,
    after: str | None = None,
    limit: int,
) -> list[Node]:
    """Nodes by kind and/or key prefix, in key (code point) order."""
    sql = f"SELECT {_rows.NODE_COLUMNS}{_rows.NODE_FROM} WHERE n.snapshot_id = %(s)s"
    params: dict[str, Any] = {"s": snapshot_id, "limit": limit}
    if kinds:
        sql += " AND n.kind = ANY(%(kinds)s)"
        params["kinds"] = [k.value for k in kinds]
    if prefix:
        sql += " AND n.key LIKE %(prefix)s"
        params["prefix"] = _escape_like(prefix) + "%"
    if after is not None:
        sql += " AND n.key > %(after)s"
        params["after"] = after
    rows = conn.execute(f"{sql} ORDER BY n.key LIMIT %(limit)s", params).fetchall()
    return [_rows.node(r) for r in rows]


def search_nodes(
    conn: Connection,
    snapshot_id: int,
    query: str,
    *,
    kinds: Sequence[NodeKind] = (),
    after: SearchSortKey | None = None,
    limit: int,
) -> list[SearchMatch]:
    """Nodes whose name or key contains ``query``, ignoring case; best matches first.

    Ranks: exact name, name prefix, name substring, then key substring (which is
    how paths are searched). Not indexed: this scans the snapshot's nodes.
    """
    escaped = _escape_like(query.lower())
    sql = (
        f"SELECT {_rows.NODE_COLUMNS}, m.rank{_rows.NODE_FROM} "
        "CROSS JOIN LATERAL (SELECT CASE"
        " WHEN lower(n.name) = %(q)s THEN 0"
        " WHEN lower(n.name) LIKE %(prefix)s THEN 1"
        " WHEN lower(n.name) LIKE %(contains)s THEN 2"
        " WHEN lower(n.key) LIKE %(contains)s THEN 3 END AS rank) m "
        "WHERE n.snapshot_id = %(s)s AND m.rank IS NOT NULL"
    )
    params: dict[str, Any] = {"s": snapshot_id, "q": query.lower(), "prefix": escaped + "%",
                              "contains": "%" + escaped + "%", "limit": limit}  # fmt: skip
    if kinds:
        sql += " AND n.kind = ANY(%(kinds)s)"
        params["kinds"] = [k.value for k in kinds]
    if after is not None:
        sql += " AND (m.rank, n.key) > (%(rank)s, %(after)s)"
        params.update(rank=after[0], after=after[1])
    rows = conn.execute(f"{sql} ORDER BY m.rank, n.key LIMIT %(limit)s", params).fetchall()
    return [SearchMatch(_rows.node(r), int(r[_rows.NODE_WIDTH])) for r in rows]


def get_tags(
    conn: Connection, snapshot_id: int, keys: Sequence[str]
) -> dict[str, list[tuple[ClassificationFacet, str]]]:
    """(facet, value) of every classification of several nodes, in one query."""
    rows = conn.execute(
        "SELECT cn.key, c.facet, c.value FROM classification c "
        "JOIN node cn ON cn.snapshot_id = c.snapshot_id AND cn.id = c.node_id "
        "WHERE c.snapshot_id = %s AND cn.key = ANY(%s) ORDER BY cn.key, c.facet, c.value",
        (snapshot_id, list(keys)),
    ).fetchall()
    tags: dict[str, list[tuple[ClassificationFacet, str]]] = {}
    for key, facet, value in rows:
        tags.setdefault(key, []).append((ClassificationFacet(facet), value))
    return tags


def get_classifications(conn: Connection, snapshot_id: int, key: str) -> list[Classification]:
    rows = conn.execute(
        f"SELECT {_rows.CLASSIFICATION_COLUMNS}{_rows.CLASSIFICATION_FROM} "
        "WHERE c.snapshot_id = %s AND cn.key = %s ORDER BY c.facet, c.value",
        (snapshot_id, key),
    ).fetchall()
    return [_rows.classification(r) for r in rows]


# ---------------------------------------------------------------------------
# Edges


def get_edges(
    conn: Connection,
    snapshot_id: int,
    key: str,
    *,
    direction: Direction | None = None,
    kinds: Sequence[EdgeKind] = (),
    after: EdgeSortKey | None = None,
    limit: int,
) -> list[NodeEdge]:
    """Stored edges that start (``OUT``) or end (``IN``) at ``key``; both when
    ``direction`` is None: outgoing first, then incoming, each by kind and peer key.

    Nothing is inverted: an incoming edge is returned with its own source and
    target. A self-loop (recursive CALLS) is listed once in each direction.
    """
    params: dict[str, Any] = {"s": snapshot_id, "key": key, "limit": limit}
    kind_filter = ""
    if kinds:
        kind_filter = " AND e.kind = ANY(%(kinds)s)"
        params["kinds"] = [k.value for k in kinds]

    def side(dir_: int, focus: str, peer: str) -> str:
        return (
            f"SELECT {_rows.EDGE_COLUMNS}, {dir_} AS dir, {peer}.key AS peer_key, "
            f"{peer}.kind AS peer_kind, {peer}.name AS peer_name{_rows.EDGE_FROM} "
            f"WHERE e.snapshot_id = %(s)s AND e.{focus} = {_NODE_ID}{kind_filter}"
        )

    parts = []
    if direction in (None, Direction.OUT):
        parts.append(side(0, "source_id", "dst"))
    if direction in (None, Direction.IN):
        parts.append(side(1, "target_id", "src"))
    sql = f"SELECT * FROM ({' UNION ALL '.join(parts)}) x"
    if after is not None:
        sql += " WHERE (dir, kind, peer_key) > (%(dir)s, %(kind)s, %(peer)s)"
        params.update(dir=after[0], kind=after[1], peer=after[2])
    rows = conn.execute(f"{sql} ORDER BY dir, kind, peer_key LIMIT %(limit)s", params).fetchall()
    width = _rows.EDGE_WIDTH
    return [
        NodeEdge(
            edge=_rows.edge(r),
            direction=Direction.OUT if r[width] == 0 else Direction.IN,
            peer=NodeBrief(r[width + 1], NodeKind(r[width + 2]), r[width + 3]),
        )
        for r in rows
    ]


def edge_counts(conn: Connection, snapshot_id: int, key: str) -> dict[EdgeKind, tuple[int, int]]:
    """(outgoing, incoming) edge counts of one node, per kind that occurs."""
    rows = conn.execute(
        "SELECT e.kind, count(*) FILTER (WHERE e.source_id = n.id), "
        "count(*) FILTER (WHERE e.target_id = n.id) "
        "FROM node n JOIN edge e ON e.snapshot_id = n.snapshot_id "
        "AND (e.source_id = n.id OR e.target_id = n.id) "
        "WHERE n.snapshot_id = %s AND n.key = %s GROUP BY e.kind ORDER BY e.kind",
        (snapshot_id, key),
    ).fetchall()
    return {EdgeKind(r[0]): (int(r[1]), int(r[2])) for r in rows}


def get_edge(conn: Connection, snapshot_id: int, ref: EdgeRef) -> Edge | None:
    row = conn.execute(
        f"SELECT {_rows.EDGE_COLUMNS}{_rows.EDGE_FROM} WHERE e.snapshot_id = %s "
        "AND src.key = %s AND e.kind = %s AND dst.key = %s",
        (snapshot_id, ref.source_key, ref.kind.value, ref.target_key),
    ).fetchone()
    return None if row is None else _rows.edge(row)


# ---------------------------------------------------------------------------
# Diagnostics


def get_diagnostics(conn: Connection, snapshot_id: int, key: str) -> Diagnostics:
    """Unresolved references whose source or file is the node, and issues of a file."""
    unresolved = conn.execute(
        f"SELECT {_rows.UNRESOLVED_COLUMNS}{_rows.UNRESOLVED_FROM} "
        "WHERE u.snapshot_id = %(s)s AND (us.key = %(key)s OR uf.key = %(key)s) "
        "ORDER BY uf.key, u.start_line, u.start_col, u.raw_text",
        {"s": snapshot_id, "key": key},
    ).fetchall()
    issues = conn.execute(
        f"SELECT {_rows.ISSUE_COLUMNS}{_rows.ISSUE_FROM} "
        "WHERE i.snapshot_id = %s AND f.key = %s "
        "ORDER BY i.start_line NULLS FIRST, i.start_col, i.code",
        (snapshot_id, key),
    ).fetchall()
    return Diagnostics(
        unresolved_references=tuple(_rows.unresolved(r) for r in unresolved),
        extraction_issues=tuple(_rows.issue(r) for r in issues),
    )


def diagnostic_counts(conn: Connection, snapshot_id: int, key: str) -> tuple[int, int]:
    """(unresolved references, extraction issues), counted as ``get_diagnostics`` lists them."""
    row = conn.execute(
        f"SELECT (SELECT count(*) FROM unresolved_reference u WHERE u.snapshot_id = %(s)s "
        f"AND (u.source_id = {_NODE_ID} OR u.file_id = {_NODE_ID})), "
        f"(SELECT count(*) FROM extraction_issue i WHERE i.snapshot_id = %(s)s "
        f"AND i.file_id = {_NODE_ID})",
        {"s": snapshot_id, "key": key},
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
