"""Snapshot-independent references to CIM records.

Records reference nodes by ``key`` and edges by ``(kind, source_key, target_key)``
(an edge is unique per snapshot on that triple). Database ids are a storage
detail and never appear in the contract.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ._base import CIMModel
from .enums import EdgeKind


class NodeRef(CIMModel):
    ref: Literal["node"]
    key: str


class EdgeRef(CIMModel):
    ref: Literal["edge"]
    kind: EdgeKind
    source_key: str
    target_key: str

    @classmethod
    def of(cls, kind: EdgeKind, source_key: str, target_key: str) -> EdgeRef:
        return cls(ref="edge", kind=kind, source_key=source_key, target_key=target_key)


class SpanRef(CIMModel):
    """A line range of a file, for citing source text directly."""

    ref: Literal["span"]
    file_key: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> SpanRef:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


Citation = Annotated[NodeRef | EdgeRef | SpanRef, Field(discriminator="ref")]
