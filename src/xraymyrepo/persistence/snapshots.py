"""Store completed CIM v1 snapshot documents in PostgreSQL and load them back.

The database schema is ``db/migrations``; this module only maps a validated
``SnapshotDocument`` onto it and back. It does not re-check CIM invariants: the
document validated them when it was constructed, the SQL constraints check them
again on insert, and ``load_snapshot`` re-validates by constructing a document.

Lifecycle: ``persist_snapshot`` inserts the snapshot as ``pending``, bulk-loads
every row and marks it ``complete``, all in one transaction. Any failure rolls the
whole snapshot back, so a snapshot is never visible half-written. Once complete,
the database rejects every change to the snapshot and its rows
(``snapshot_guard_immutable``, ``snapshot_child_guard_immutable``).

Round trip: ``load_snapshot(persist_snapshot(doc)) == doc.canonical()``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NamedTuple

import psycopg
from pydantic import TypeAdapter

from xraymyrepo.cim import (
    CoverageDeclaration,
    NodeKind,
    SnapshotDocument,
    SnapshotIdentity,
    SnapshotStatus,
)
from xraymyrepo.cim.evidence import FactEvidence, SourceSpan
from xraymyrepo.cim.nodes import Node

Connection = psycopg.Connection[Any]

_EVIDENCE: TypeAdapter[tuple[FactEvidence, ...]] = TypeAdapter(tuple[FactEvidence, ...])
_COVERAGE: TypeAdapter[tuple[CoverageDeclaration, ...]] = TypeAdapter(
    tuple[CoverageDeclaration, ...]
)
_CANDIDATES: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])
_FILE_SCOPED_KINDS = frozenset({NodeKind.TYPE, NodeKind.FUNCTION, NodeKind.ENDPOINT})


class PersistenceError(Exception):
    """A snapshot could not be stored or loaded."""


class SnapshotExistsError(PersistenceError):
    """A snapshot with the same identity is already stored. Re-analysis of the same
    (repository, commit, extractor version, config) never replaces it."""

    def __init__(self, snapshot_id: int, status: SnapshotStatus) -> None:
        super().__init__(f"snapshot {snapshot_id} with this identity already exists ({status})")
        self.snapshot_id = snapshot_id
        self.status = status


class RepositoryIdentityError(PersistenceError):
    """The repository is stored with different letter case for owner or name."""


class SnapshotNotFoundError(PersistenceError):
    pass


class SnapshotNotCompleteError(PersistenceError):
    pass


class StoredSnapshot(NamedTuple):
    id: int
    status: SnapshotStatus


# ---------------------------------------------------------------------------
# Writing


def persist_snapshot(conn: Connection, doc: SnapshotDocument) -> int:
    """Store ``doc`` as a complete snapshot and return its id.

    Runs in one transaction (a savepoint when the caller already has one open, in
    which case the snapshot commits with the caller's transaction).
    Raises ``SnapshotExistsError`` if the identity is already stored.
    """
    with conn.transaction(), conn.cursor() as cur:
        snapshot_id = _insert_pending(cur, doc)
        producer_ids = _attach_producers(cur, snapshot_id, doc)
        node_ids = _insert_nodes(cur, snapshot_id, doc, producer_ids)
        _insert_edges(cur, snapshot_id, doc, node_ids)
        _insert_classifications(cur, snapshot_id, doc, node_ids)
        _insert_unresolved_references(cur, snapshot_id, doc, node_ids, producer_ids)
        _insert_extraction_issues(cur, snapshot_id, doc, node_ids, producer_ids)
        _complete_snapshot(cur, snapshot_id)
    return snapshot_id


def _insert_pending(cur: psycopg.Cursor[Any], doc: SnapshotDocument) -> int:
    snap = doc.snapshot
    repository_id = _repository_id(cur, doc)
    row = cur.execute(
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config, coverage) VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT ON CONSTRAINT snapshot_identity_key DO NOTHING RETURNING id",
        (repository_id, snap.commit_sha, snap.extractor_version, snap.config_hash,
         doc.schema_version, doc.config.model_dump_json(),
         _COVERAGE.dump_json(doc.coverage).decode()),
    ).fetchone()  # fmt: skip
    if row is None:
        existing = _find(cur, repository_id, snap)
        assert existing is not None
        raise SnapshotExistsError(existing.id, existing.status)
    return int(row[0])


def _repository_id(cur: psycopg.Cursor[Any], doc: SnapshotDocument) -> int:
    repo = doc.snapshot.repository
    cur.execute(
        "INSERT INTO repository (host, owner, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (repo.host, repo.owner, repo.name),
    )
    row = cur.execute(
        "SELECT id, owner, name FROM repository "
        "WHERE host = %s AND lower(owner) = lower(%s) AND lower(name) = lower(%s)",
        (repo.host, repo.owner, repo.name),
    ).fetchone()
    assert row is not None
    if (row[1], row[2]) != (repo.owner, repo.name):
        # Storing the document under the existing spelling would break the round trip.
        raise RepositoryIdentityError(
            f"repository {repo.slug!r} is stored as {repo.host}/{row[1]}/{row[2]}"
        )
    return int(row[0])


def _attach_producers(
    cur: psycopg.Cursor[Any], snapshot_id: int, doc: SnapshotDocument
) -> dict[str, int]:
    ids: dict[str, int] = {}
    for producer in doc.producers:
        values = (producer.name, producer.version, producer.config_hash)
        cur.execute(
            "INSERT INTO producer (name, version, config_hash) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            values,
        )
        row = cur.execute(
            "SELECT id FROM producer WHERE name = %s AND version = %s AND config_hash = %s",
            values,
        ).fetchone()
        assert row is not None
        ids[producer.name] = int(row[0])
    _copy(
        cur,
        "COPY snapshot_producer (snapshot_id, producer_id, producer_name) FROM STDIN",
        ((snapshot_id, producer_id, name) for name, producer_id in ids.items()),
    )
    return ids


def _insert_nodes(
    cur: psycopg.Cursor[Any],
    snapshot_id: int,
    doc: SnapshotDocument,
    producer_ids: dict[str, int],
) -> dict[str, int]:
    """COPY every node in one statement and return their ids by key.

    Ids are reserved from the column's identity sequence first, so ``parent_id``
    and ``file_id`` are known before the rows exist (COPY writes identity values
    as given). Foreign keys are checked at the end of the statement, so row order
    does not matter. Ids follow key order, which keeps reloads in a stable order.
    """
    nodes = sorted(doc.nodes, key=lambda n: n.key)
    reserved = cur.execute(
        "SELECT nextval(pg_get_serial_sequence('node', 'id')) FROM generate_series(1, %s)",
        (len(nodes),),
    ).fetchall()
    ids = dict(zip((n.key for n in nodes), sorted(int(r[0]) for r in reserved), strict=True))
    by_key = doc.nodes_by_key
    depths: dict[str, int] = {}

    def depth(node: Node) -> int:
        if node.key not in depths:
            parent = node.parent_key
            depths[node.key] = 0 if parent is None else depth(by_key[parent]) + 1
        return depths[node.key]

    def file_id(node: Node) -> int | None:
        if node.kind not in _FILE_SCOPED_KINDS:
            return None
        current = node
        while current.kind is not NodeKind.FILE:
            assert current.parent_key is not None
            current = by_key[current.parent_key]
        return ids[current.key]

    _copy(
        cur,
        "COPY node (snapshot_id, id, key, kind, name, parent_id, depth, file_id, language, "
        "start_line, start_col, end_line, end_col, basis, confidence, producer_id, rule, "
        "extraction_status, blob_sha, content_hash, signature_hash, evidence, attributes) "
        "FROM STDIN",
        ((snapshot_id, ids[n.key], n.key, n.kind.value, n.name,
          None if n.parent_key is None else ids[n.parent_key], depth(n), file_id(n),
          n.language, *_span(n.span), n.basis.value, n.confidence.value,
          producer_ids[n.provenance.producer], n.provenance.rule,
          None if n.extraction_status is None else n.extraction_status.value,
          n.blob_sha, n.content_hash, n.signature_hash,
          _evidence(n.evidence), n.attributes.model_dump_json())
         for n in nodes),
    )  # fmt: skip
    return ids


def _insert_edges(
    cur: psycopg.Cursor[Any], snapshot_id: int, doc: SnapshotDocument, node_ids: dict[str, int]
) -> None:
    _copy(
        cur,
        "COPY edge (snapshot_id, kind, source_id, target_id, basis, confidence, "
        "occurrence_count, ambiguity_group, contested, evidence, attributes) FROM STDIN",
        ((snapshot_id, e.kind.value, node_ids[e.source_key], node_ids[e.target_key],
          e.basis.value, e.confidence.value, e.occurrence_count, e.ambiguity_group,
          e.contested, _evidence(e.evidence), e.attributes.model_dump_json())
         for e in doc.edges),
    )  # fmt: skip


def _insert_classifications(
    cur: psycopg.Cursor[Any], snapshot_id: int, doc: SnapshotDocument, node_ids: dict[str, int]
) -> None:
    _copy(
        cur,
        "COPY classification (snapshot_id, node_id, facet, value, basis, confidence, "
        "contested, evidence) FROM STDIN",
        ((snapshot_id, node_ids[c.node_key], c.facet.value, c.value, c.basis.value,
          c.confidence.value, c.contested, _evidence(c.evidence))
         for c in doc.classifications),
    )  # fmt: skip


def _insert_unresolved_references(
    cur: psycopg.Cursor[Any],
    snapshot_id: int,
    doc: SnapshotDocument,
    node_ids: dict[str, int],
    producer_ids: dict[str, int],
) -> None:
    _copy(
        cur,
        "COPY unresolved_reference (snapshot_id, source_id, file_id, ref_kind, raw_text, "
        "start_line, start_col, end_line, end_col, reason, candidates, producer_id, rule) "
        "FROM STDIN",
        ((snapshot_id, node_ids[u.source_key], node_ids[u.file_key], u.ref_kind.value,
          u.raw_text, u.start_line, u.start_col, u.end_line, u.end_col, u.reason.value,
          _CANDIDATES.dump_json(u.candidates).decode(),
          producer_ids[u.provenance.producer], u.provenance.rule)
         for u in doc.unresolved_references),
    )  # fmt: skip


def _insert_extraction_issues(
    cur: psycopg.Cursor[Any],
    snapshot_id: int,
    doc: SnapshotDocument,
    node_ids: dict[str, int],
    producer_ids: dict[str, int],
) -> None:
    _copy(
        cur,
        "COPY extraction_issue (snapshot_id, file_id, severity, code, message, start_line, "
        "start_col, end_line, end_col, producer_id, rule) FROM STDIN",
        ((snapshot_id, None if i.file_key is None else node_ids[i.file_key], i.severity.value,
          i.code, i.message, *_span(i.span), producer_ids[i.provenance.producer],
          i.provenance.rule)
         for i in doc.extraction_issues),
    )  # fmt: skip


def _complete_snapshot(cur: psycopg.Cursor[Any], snapshot_id: int) -> None:
    """The one-way transition pending -> complete. Commits with the rows it covers."""
    cur.execute(
        "UPDATE snapshot SET status = 'complete', finished_at = now() "
        "WHERE id = %s AND status = 'pending'",
        (snapshot_id,),
    )
    if cur.rowcount != 1:
        raise PersistenceError(f"snapshot {snapshot_id} is no longer pending")


def _copy(cur: psycopg.Cursor[Any], statement: str, rows: Iterable[tuple[Any, ...]]) -> None:
    with cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


def _evidence(evidence: tuple[FactEvidence, ...]) -> str:
    return _EVIDENCE.dump_json(evidence).decode()


def _span(span: SourceSpan | None) -> tuple[int | None, ...]:
    if span is None:
        return (None, None, None, None)
    return (span.start_line, span.start_col, span.end_line, span.end_col)


# ---------------------------------------------------------------------------
# Reading


def find_snapshot(conn: Connection, identity: SnapshotIdentity) -> StoredSnapshot | None:
    """The stored snapshot with this identity, in any status."""
    with conn.cursor() as cur:
        repo = identity.repository
        row = cur.execute(
            "SELECT id FROM repository WHERE host = %s AND owner = %s AND name = %s",
            (repo.host, repo.owner, repo.name),
        ).fetchone()
        return None if row is None else _find(cur, int(row[0]), identity)


def _find(
    cur: psycopg.Cursor[Any], repository_id: int, identity: SnapshotIdentity
) -> StoredSnapshot | None:
    row = cur.execute(
        "SELECT id, status FROM snapshot WHERE repository_id = %s AND commit_sha = %s "
        "AND extractor_version = %s AND config_hash = %s",
        (repository_id, identity.commit_sha, identity.extractor_version, identity.config_hash),
    ).fetchone()
    return None if row is None else StoredSnapshot(int(row[0]), SnapshotStatus(row[1]))


def load_snapshot(conn: Connection, snapshot_id: int) -> SnapshotDocument:
    """Load a complete snapshot as a validated document, in canonical order.

    Seven queries regardless of size: the header, the producers and one per row
    table. Node ids are mapped back to keys in memory. Constructing the document
    re-runs every CIM check, so a reload is as trustworthy as the original.
    """
    with conn.transaction(), conn.cursor() as cur:
        header = cur.execute(
            "SELECT s.status, s.cim_schema_version, r.host, r.owner, r.name, s.commit_sha, "
            "s.extractor_version, s.config_hash, s.config, s.coverage "
            "FROM snapshot s JOIN repository r ON r.id = s.repository_id WHERE s.id = %s",
            (snapshot_id,),
        ).fetchone()
        if header is None:
            raise SnapshotNotFoundError(f"snapshot {snapshot_id} does not exist")
        status, schema, host, owner, name, commit, version, config_hash, config, coverage = header
        if status != SnapshotStatus.COMPLETE:
            raise SnapshotNotCompleteError(f"snapshot {snapshot_id} is {status}")

        producers = cur.execute(
            "SELECT p.id, p.name, p.version, p.config_hash FROM snapshot_producer sp "
            "JOIN producer p ON p.id = sp.producer_id WHERE sp.snapshot_id = %s ORDER BY p.name",
            (snapshot_id,),
        ).fetchall()
        producer_names = {int(r[0]): r[1] for r in producers}

        node_rows = cur.execute(
            "SELECT id, key, kind, name, parent_id, language, start_line, start_col, end_line, "
            "end_col, basis, confidence, producer_id, rule, extraction_status, blob_sha, "
            "content_hash, signature_hash, evidence, attributes "
            "FROM node WHERE snapshot_id = %s ORDER BY id",
            (snapshot_id,),
        ).fetchall()
        keys = {int(r[0]): r[1] for r in node_rows}

        def provenance(producer_id: int, rule: str | None) -> dict[str, Any]:
            return {"producer": producer_names[producer_id], "rule": rule}

        nodes = [
            {"key": key, "kind": kind, "name": node_name,
             "parent_key": None if parent_id is None else keys[parent_id],
             "language": language, "span": _span_dict(sl, sc, el, ec),
             "basis": basis, "confidence": confidence,
             "provenance": provenance(producer_id, rule), "evidence": evidence,
             "extraction_status": extraction_status, "blob_sha": blob_sha,
             "content_hash": content_hash, "signature_hash": signature_hash,
             "attributes": attributes}
            for (_, key, kind, node_name, parent_id, language, sl, sc, el, ec, basis,
                 confidence, producer_id, rule, extraction_status, blob_sha, content_hash,
                 signature_hash, evidence, attributes) in node_rows
        ]  # fmt: skip

        edges = [
            {"kind": kind, "source_key": keys[source_id], "target_key": keys[target_id],
             "basis": basis, "confidence": confidence, "occurrence_count": occurrences,
             "evidence": evidence, "ambiguity_group": ambiguity_group,
             "contested": contested, "attributes": attributes}
            for (kind, source_id, target_id, basis, confidence, occurrences, ambiguity_group,
                 contested, evidence, attributes) in cur.execute(
                "SELECT kind, source_id, target_id, basis, confidence, occurrence_count, "
                "ambiguity_group, contested, evidence, attributes "
                "FROM edge WHERE snapshot_id = %s ORDER BY id", (snapshot_id,))
        ]  # fmt: skip

        classifications = [
            {"node_key": keys[node_id], "facet": facet, "value": value, "basis": basis,
             "confidence": confidence, "contested": contested, "evidence": evidence}
            for (node_id, facet, value, basis, confidence, contested, evidence) in cur.execute(
                "SELECT node_id, facet, value, basis, confidence, contested, evidence "
                "FROM classification WHERE snapshot_id = %s ORDER BY id", (snapshot_id,))
        ]  # fmt: skip

        unresolved = [
            {"source_key": keys[source_id], "file_key": keys[file_id], "ref_kind": ref_kind,
             "raw_text": raw_text, "start_line": sl, "start_col": sc, "end_line": el,
             "end_col": ec, "reason": reason, "candidates": candidates,
             "provenance": provenance(producer_id, rule)}
            for (source_id, file_id, ref_kind, raw_text, sl, sc, el, ec, reason, candidates,
                 producer_id, rule) in cur.execute(
                "SELECT source_id, file_id, ref_kind, raw_text, start_line, start_col, "
                "end_line, end_col, reason, candidates, producer_id, rule "
                "FROM unresolved_reference WHERE snapshot_id = %s ORDER BY id", (snapshot_id,))
        ]  # fmt: skip

        issues = [
            {"file_key": None if file_id is None else keys[file_id], "severity": severity,
             "code": code, "message": message, "span": _span_dict(sl, sc, el, ec),
             "provenance": provenance(producer_id, rule)}
            for (file_id, severity, code, message, sl, sc, el, ec, producer_id, rule)
            in cur.execute(
                "SELECT file_id, severity, code, message, start_line, start_col, end_line, "
                "end_col, producer_id, rule "
                "FROM extraction_issue WHERE snapshot_id = %s ORDER BY id", (snapshot_id,))
        ]  # fmt: skip

    document = SnapshotDocument.model_validate(
        {
            "schema_version": schema,
            "snapshot": {
                "repository": {"host": host, "owner": owner, "name": name},
                "commit_sha": commit,
                "extractor_version": version,
                "config_hash": config_hash,
            },
            "config": config,
            "producers": [{"name": r[1], "version": r[2], "config_hash": r[3]} for r in producers],
            "coverage": coverage,
            "nodes": nodes,
            "edges": edges,
            "classifications": classifications,
            "unresolved_references": unresolved,
            "extraction_issues": issues,
        }
    )
    return document.canonical()


def _span_dict(
    start_line: int | None, start_col: int | None, end_line: int | None, end_col: int | None
) -> dict[str, int | None] | None:
    if start_line is None:
        return None
    return {"start_line": start_line, "start_col": start_col, "end_line": end_line,
            "end_col": end_col}  # fmt: skip
