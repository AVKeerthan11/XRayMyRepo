"""Interpretation tier: append-only AI annotations.

Annotations explain or name things in the graph; they never are the graph. An
annotation has no way to declare nodes, edges or classifications (the model
forbids extra fields), its basis is always ``interpreted``, and it must cite the
facts it is based on. Revising an annotation appends a new one and points the
old one at it through ``superseded_by``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from ._base import CIMModel, Text
from .enums import AnnotationKind, Basis
from .refs import Citation, EdgeRef, NodeRef
from .snapshot import SnapshotIdentity

AnnotationSubject = Annotated[NodeRef | EdgeRef, Field(discriminator="ref")]


class Annotation(CIMModel):
    id: UUID
    snapshot: SnapshotIdentity
    subject: AnnotationSubject
    """A node (including a group, by its group key) or an edge."""
    kind: AnnotationKind
    content: str = Field(min_length=1)
    model: Text
    prompt_version: Text
    citations: tuple[Citation, ...] = Field(min_length=1)
    created_at: datetime
    superseded_by: UUID | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Annotation:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.superseded_by == self.id:
            raise ValueError("an annotation cannot supersede itself")
        return self

    @property
    def basis(self) -> Basis:
        return Basis.INTERPRETED
