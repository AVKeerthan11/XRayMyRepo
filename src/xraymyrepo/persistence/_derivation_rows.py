"""Row -> CIM derivation record mapping, shared by the full load and the read queries.

As in ``_rows``: each ``*_COLUMNS``/``*_FROM`` pair selects one record per row with
ids resolved to keys (group keys, node keys) by joins; the mapping functions read
only the first columns, so queries may append their own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from xraymyrepo.cim import Finding, Group, GroupEdge, InferredLayer

GROUP_COLUMNS = "g.key, parent.key, g.label, g.basis, g.confidence, g.producer, g.rule, g.evidence"
GROUP_FROM = (
    " FROM derived_group g LEFT JOIN derived_group parent"
    " ON parent.run_id = g.run_id AND parent.id = g.parent_id"
)

GROUP_EDGE_COLUMNS = "src.key, dst.key, e.edge_kind, e.count, e.basis_counts, e.sample_edge_refs"
GROUP_EDGE_FROM = (
    " FROM group_edge e"
    " JOIN derived_group src ON src.run_id = e.run_id AND src.id = e.source_group_id"
    " JOIN derived_group dst ON dst.run_id = e.run_id AND dst.id = e.target_group_id"
)

FINDING_COLUMNS = "f.rule, f.severity, f.subject_keys, f.message, f.basis, f.confidence, f.evidence"
FINDING_FROM = " FROM finding f"

LAYER_COLUMNS = "ln.key, l.layer, l.basis, l.confidence, l.producer, l.rule, l.evidence"
LAYER_FROM = (
    " FROM inferred_layer l JOIN node ln ON ln.snapshot_id = l.snapshot_id AND ln.id = l.node_id"
)


def provenance(producer: str, rule: str | None) -> dict[str, Any]:
    return {"producer": producer, "rule": rule}


def group_data(row: Sequence[Any]) -> dict[str, Any]:
    key, parent, label, basis, confidence, producer, rule, evidence = row[:8]
    return {"key": key, "parent_key": parent, "label": label, "basis": basis,
            "confidence": confidence, "provenance": provenance(producer, rule),
            "evidence": evidence}  # fmt: skip


def group(row: Sequence[Any]) -> Group:
    return Group.model_validate(group_data(row))


def group_edge_data(row: Sequence[Any]) -> dict[str, Any]:
    source, target, kind, count, basis_counts, samples = row[:6]
    return {"source_group_key": source, "target_group_key": target, "edge_kind": kind,
            "count": count, "basis_counts": basis_counts,
            "sample_edge_refs": samples}  # fmt: skip


def group_edge(row: Sequence[Any]) -> GroupEdge:
    return GroupEdge.model_validate(group_edge_data(row))


def finding_data(row: Sequence[Any]) -> dict[str, Any]:
    rule, severity, subjects, message, basis, confidence, evidence = row[:7]
    return {"rule": rule, "severity": severity, "subject_keys": subjects, "message": message,
            "basis": basis, "confidence": confidence, "evidence": evidence}  # fmt: skip


def finding(row: Sequence[Any]) -> Finding:
    return Finding.model_validate(finding_data(row))


def layer_data(row: Sequence[Any]) -> dict[str, Any]:
    key, layer, basis, confidence, producer, rule, evidence = row[:7]
    return {"node_key": key, "layer": layer, "basis": basis, "confidence": confidence,
            "provenance": provenance(producer, rule), "evidence": evidence}  # fmt: skip


def layer(row: Sequence[Any]) -> InferredLayer:
    return InferredLayer.model_validate(layer_data(row))
