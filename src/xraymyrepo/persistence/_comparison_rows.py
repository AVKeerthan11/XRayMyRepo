"""Row -> CIM comparison record mapping, shared by the full load and the read queries.

As in ``_derivation_rows``: each ``*_COLUMNS``/``*_FROM`` pair selects one record per
row with node and edge ids resolved to keys by joins; the mapping functions read only
the first columns, so queries may append their own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

NODE_CHANGE_COLUMNS = (
    "r.change, r.kind, f.key, t.key, r.match_method, r.confidence, r.move_ambiguous"
)
NODE_CHANGE_FROM = (
    " FROM node_change r"
    " LEFT JOIN node f ON f.snapshot_id = r.from_snapshot_id AND f.id = r.from_node_id"
    " LEFT JOIN node t ON t.snapshot_id = r.to_snapshot_id AND t.id = r.to_node_id"
)

EDGE_CHANGE_COLUMNS = "r.change, r.kind, r.confidence, fs.key, ft.key, ts.key, tt.key"
EDGE_CHANGE_FROM = (
    " FROM edge_change r"
    " LEFT JOIN edge fe ON fe.snapshot_id = r.from_snapshot_id AND fe.id = r.from_edge_id"
    " LEFT JOIN node fs ON fs.snapshot_id = fe.snapshot_id AND fs.id = fe.source_id"
    " LEFT JOIN node ft ON ft.snapshot_id = fe.snapshot_id AND ft.id = fe.target_id"
    " LEFT JOIN edge te ON te.snapshot_id = r.to_snapshot_id AND te.id = r.to_edge_id"
    " LEFT JOIN node ts ON ts.snapshot_id = te.snapshot_id AND ts.id = te.source_id"
    " LEFT JOIN node tt ON tt.snapshot_id = te.snapshot_id AND tt.id = te.target_id"
)


def node_change_data(row: Sequence[Any]) -> dict[str, Any]:
    change, kind, from_key, to_key, method, confidence, ambiguous = row[:7]
    return {"change": change, "kind": kind, "from_key": from_key, "to_key": to_key,
            "match_method": method, "confidence": confidence,
            "move_ambiguous": ambiguous}  # fmt: skip


def _ref(kind: str, source: str | None, target: str | None) -> dict[str, str] | None:
    if source is None:
        return None
    assert target is not None
    return {"ref": "edge", "kind": kind, "source_key": source, "target_key": target}


def edge_change_data(row: Sequence[Any]) -> dict[str, Any]:
    change, kind, confidence, from_source, from_target, to_source, to_target = row[:7]
    return {"change": change, "kind": kind, "confidence": confidence,
            "from_ref": _ref(kind, from_source, from_target),
            "to_ref": _ref(kind, to_source, to_target)}  # fmt: skip
