"""Row -> CIM record mapping, shared by the full load and the read queries.

Each ``*_COLUMNS``/``*_FROM`` pair selects one CIM record per row and resolves the
database ids to what the CIM uses instead (node keys, producer names) with joins,
so any single row maps to a record without further lookups. Every column is
aliased, so queries can wrap these selects or append their own columns after
them; the mapping functions read only the first ``*_WIDTH`` columns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from xraymyrepo.cim import Classification, Edge, ExtractionIssue, UnresolvedReference
from xraymyrepo.cim.nodes import Node

NODE_COLUMNS = (
    "n.key AS key, n.kind AS kind, n.name AS name, parent.key AS parent_key, "
    "n.language AS language, n.start_line AS start_line, n.start_col AS start_col, "
    "n.end_line AS end_line, n.end_col AS end_col, n.basis AS basis, "
    "n.confidence AS confidence, nsp.producer_name AS producer, n.rule AS rule, "
    "n.extraction_status AS extraction_status, n.blob_sha AS blob_sha, "
    "n.content_hash AS content_hash, n.signature_hash AS signature_hash, "
    "n.evidence AS evidence, n.attributes AS attributes"
)
NODE_FROM = (
    " FROM node n"
    " JOIN snapshot_producer nsp"
    " ON nsp.snapshot_id = n.snapshot_id AND nsp.producer_id = n.producer_id"
    " LEFT JOIN node parent ON parent.snapshot_id = n.snapshot_id AND parent.id = n.parent_id"
)
NODE_WIDTH = 19

EDGE_COLUMNS = (
    "e.kind AS kind, src.key AS source_key, dst.key AS target_key, e.basis AS basis, "
    "e.confidence AS confidence, e.occurrence_count AS occurrence_count, "
    "e.ambiguity_group AS ambiguity_group, e.contested AS contested, "
    "e.evidence AS evidence, e.attributes AS attributes"
)
EDGE_FROM = (
    " FROM edge e"
    " JOIN node src ON src.snapshot_id = e.snapshot_id AND src.id = e.source_id"
    " JOIN node dst ON dst.snapshot_id = e.snapshot_id AND dst.id = e.target_id"
)
EDGE_WIDTH = 10

CLASSIFICATION_COLUMNS = (
    "cn.key AS node_key, c.facet AS facet, c.value AS value, c.basis AS basis, "
    "c.confidence AS confidence, c.contested AS contested, c.evidence AS evidence"
)
CLASSIFICATION_FROM = (
    " FROM classification c JOIN node cn ON cn.snapshot_id = c.snapshot_id AND cn.id = c.node_id"
)

UNRESOLVED_COLUMNS = (
    "us.key AS source_key, uf.key AS file_key, u.ref_kind AS ref_kind, u.raw_text AS raw_text, "
    "u.start_line AS start_line, u.start_col AS start_col, u.end_line AS end_line, "
    "u.end_col AS end_col, u.reason AS reason, u.candidates AS candidates, "
    "usp.producer_name AS producer, u.rule AS rule"
)
UNRESOLVED_FROM = (
    " FROM unresolved_reference u"
    " JOIN node us ON us.snapshot_id = u.snapshot_id AND us.id = u.source_id"
    " JOIN node uf ON uf.snapshot_id = u.snapshot_id AND uf.id = u.file_id"
    " JOIN snapshot_producer usp"
    " ON usp.snapshot_id = u.snapshot_id AND usp.producer_id = u.producer_id"
)

ISSUE_COLUMNS = (
    "f.key AS file_key, i.severity AS severity, i.code AS code, i.message AS message, "
    "i.start_line AS start_line, i.start_col AS start_col, i.end_line AS end_line, "
    "i.end_col AS end_col, isp.producer_name AS producer, i.rule AS rule"
)
ISSUE_FROM = (
    " FROM extraction_issue i"
    " LEFT JOIN node f ON f.snapshot_id = i.snapshot_id AND f.id = i.file_id"
    " JOIN snapshot_producer isp"
    " ON isp.snapshot_id = i.snapshot_id AND isp.producer_id = i.producer_id"
)


def node_data(row: Sequence[Any]) -> dict[str, Any]:
    (key, kind, name, parent_key, language, sl, sc, el, ec, basis, confidence, producer, rule,
     extraction_status, blob_sha, content_hash, signature_hash, evidence,
     attributes) = row[:NODE_WIDTH]  # fmt: skip
    return {"key": key, "kind": kind, "name": name, "parent_key": parent_key,
            "language": language, "span": span_data(sl, sc, el, ec), "basis": basis,
            "confidence": confidence, "provenance": {"producer": producer, "rule": rule},
            "evidence": evidence, "extraction_status": extraction_status,
            "blob_sha": blob_sha, "content_hash": content_hash,
            "signature_hash": signature_hash, "attributes": attributes}  # fmt: skip


def node(row: Sequence[Any]) -> Node:
    return Node.model_validate(node_data(row))


def edge_data(row: Sequence[Any]) -> dict[str, Any]:
    (kind, source_key, target_key, basis, confidence, occurrence_count, ambiguity_group,
     contested, evidence, attributes) = row[:EDGE_WIDTH]  # fmt: skip
    return {"kind": kind, "source_key": source_key, "target_key": target_key, "basis": basis,
            "confidence": confidence, "occurrence_count": occurrence_count,
            "evidence": evidence, "ambiguity_group": ambiguity_group,
            "contested": contested, "attributes": attributes}  # fmt: skip


def edge(row: Sequence[Any]) -> Edge:
    return Edge.model_validate(edge_data(row))


def classification_data(row: Sequence[Any]) -> dict[str, Any]:
    node_key, facet, value, basis, confidence, contested, evidence = row[:7]
    return {"node_key": node_key, "facet": facet, "value": value, "basis": basis,
            "confidence": confidence, "contested": contested, "evidence": evidence}  # fmt: skip


def classification(row: Sequence[Any]) -> Classification:
    return Classification.model_validate(classification_data(row))


def unresolved_data(row: Sequence[Any]) -> dict[str, Any]:
    (source_key, file_key, ref_kind, raw_text, sl, sc, el, ec, reason, candidates, producer,
     rule) = row[:12]  # fmt: skip
    return {"source_key": source_key, "file_key": file_key, "ref_kind": ref_kind,
            "raw_text": raw_text, "start_line": sl, "start_col": sc, "end_line": el,
            "end_col": ec, "reason": reason, "candidates": candidates,
            "provenance": {"producer": producer, "rule": rule}}  # fmt: skip


def unresolved(row: Sequence[Any]) -> UnresolvedReference:
    return UnresolvedReference.model_validate(unresolved_data(row))


def issue_data(row: Sequence[Any]) -> dict[str, Any]:
    file_key, severity, code, message, sl, sc, el, ec, producer, rule = row[:10]
    return {"file_key": file_key, "severity": severity, "code": code, "message": message,
            "span": span_data(sl, sc, el, ec),
            "provenance": {"producer": producer, "rule": rule}}  # fmt: skip


def issue(row: Sequence[Any]) -> ExtractionIssue:
    return ExtractionIssue.model_validate(issue_data(row))


def span_data(
    start_line: int | None, start_col: int | None, end_line: int | None, end_col: int | None
) -> dict[str, int | None] | None:
    if start_line is None:
        return None
    return {"start_line": start_line, "start_col": start_col, "end_line": end_line,
            "end_col": end_col}  # fmt: skip
