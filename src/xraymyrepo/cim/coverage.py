"""What the extractors could not see.

Absence of an edge is only evidence of absence when (a) the file was extracted
(``ExtractionStatus.FULL``), (b) the snapshot declares that the edge kind was
extracted for the file's language (``CoverageDeclaration``), and (c) no
unresolved reference of that kind exists in scope. These records make that
check possible.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from ._base import CIMModel, Slug, Text
from .enums import EdgeKind, IssueSeverity, NodeKind, ReferenceKind, UnresolvedReason
from .evidence import SourceSpan, SpanFields
from .keys import validate_key
from .provenance import Provenance


class CoverageDeclaration(CIMModel):
    """The edge kinds a snapshot attempted to extract for one language."""

    language: Slug
    edge_kinds: tuple[EdgeKind, ...] = Field(min_length=1)

    @field_validator("edge_kinds")
    @classmethod
    def _sorted_unique(cls, value: tuple[EdgeKind, ...]) -> tuple[EdgeKind, ...]:
        if list(value) != sorted(set(value)):
            raise ValueError("edge_kinds must be sorted and unique")
        return value


class UnresolvedReference(SpanFields):
    """A reference found in source whose target could not be determined."""

    source_key: str
    """Innermost named scope containing the reference (a file or a symbol in it)."""
    file_key: str
    ref_kind: ReferenceKind
    raw_text: Text
    """The reference as written: a specifier, a call expression, a base class name."""
    reason: UnresolvedReason
    candidates: tuple[str, ...] = ()
    """Candidate target keys; required (>= 2) when ``reason`` is ``ambiguous``."""
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> UnresolvedReference:
        validate_key(NodeKind.FILE, self.file_key)
        if self.reason is UnresolvedReason.AMBIGUOUS:
            if len(self.candidates) < 2:
                raise ValueError("an ambiguous reference needs at least two candidates")
        elif self.candidates:
            raise ValueError("only ambiguous references list candidates")
        if list(self.candidates) != sorted(set(self.candidates)):
            raise ValueError("candidates must be sorted and unique")
        return self


class ExtractionIssue(CIMModel):
    """A problem an extractor hit: parse errors, unsupported syntax, rejected paths."""

    file_key: str | None = None
    severity: IssueSeverity
    code: Slug
    message: Text
    span: SourceSpan | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> ExtractionIssue:
        if self.file_key is not None:
            validate_key(NodeKind.FILE, self.file_key)
        elif self.span is not None:
            raise ValueError("a span needs a file_key")
        return self
