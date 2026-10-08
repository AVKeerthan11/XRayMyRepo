"""Shapes shared by every endpoint: pages, node briefs and problems."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema

from xraymyrepo.cim import NodeKind
from xraymyrepo.cim.enums import SNAPSHOT_NODE_KINDS


def _snapshot_kind(kind: NodeKind) -> NodeKind:
    if kind not in SNAPSHOT_NODE_KINDS:
        raise ValueError(f"'{kind}' is not a snapshot node kind")
    return kind


SnapshotNodeKind = Annotated[
    NodeKind,
    AfterValidator(_snapshot_kind),
    WithJsonSchema(
        {
            "type": "string",
            "enum": [k.value for k in NodeKind if k in SNAPSHOT_NODE_KINDS],
            "title": "SnapshotNodeKind",
        }
    ),
]
"""A ``NodeKind`` that occurs in snapshots (never ``group``, a derivation-tier kind)."""


class ApiModel(BaseModel):
    """Base of every API response model. API models own the public contract; CIM value
    types (enums, spans, provenance, evidence, attributes) appear inside them as-is."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Page[T](ApiModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Opaque; pass as `cursor` to get the next page. Null on the last page."
    )


class NodeBrief(ApiModel):
    """Enough to name and link to a node."""

    key: str
    kind: SnapshotNodeKind
    name: str


class Problem(ApiModel):
    """An error, as RFC 9457 problem details (``application/problem+json``)."""

    type: str = Field(description="URN identifying the problem type: urn:xraymyrepo:problem:<code>")
    title: str
    status: int
    detail: str
    code: str = Field(description="Stable machine-readable code, e.g. node_not_found")
    instance: str | None = None
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Per-parameter details of a validation_error"
    )
