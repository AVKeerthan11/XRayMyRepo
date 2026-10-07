"""Typed evidence: why a node, edge or classification exists.

Each evidence item is one independent support for a claim. It carries the
``basis`` of the claim *as asserted through this item* and the ``provenance``
(producer + rule) that asserted it, so several producers can support one record.
A record's own basis is the strongest basis among its evidence items.

Evidence points at source; it never embeds source. Text is fetched later from the
file's blob using the span.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ._base import CIMModel, Slug, Text
from .enums import Basis, NodeKind
from .keys import validate_key
from .provenance import Provenance
from .refs import Citation, EdgeRef

_JSON_POINTER = re.compile(r"^(?:/(?:[^~/]|~[01])*)*$")


class SpanFields(CIMModel):
    """A source range. Lines are 1-based; columns are 0-based Unicode code points.

    ``end_line`` is the line containing the end of the range; ``end_col`` is
    exclusive.
    """

    start_line: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_col: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> SpanFields:
        if (self.end_line, self.end_col) < (self.start_line, self.start_col):
            raise ValueError("span end must not precede its start")
        return self


class SourceSpan(SpanFields):
    pass


def _file_key(value: str) -> str:
    validate_key(NodeKind.FILE, value)
    return value


class _EvidenceBase(CIMModel):
    basis: Basis
    provenance: Provenance

    @model_validator(mode="after")
    def _rule_required(self) -> _EvidenceBase:
        if self.basis in (Basis.HEURISTIC, Basis.INFERRED) and self.provenance.rule is None:
            raise ValueError(f"{self.basis} evidence must name the rule that produced it")
        if self.basis is Basis.INTERPRETED:
            raise ValueError("only 'ai' evidence may have basis 'interpreted'")
        return self


class SpanEvidence(_EvidenceBase, SpanFields):
    """A location in source code, e.g. an import statement or a decorator."""

    type: Literal["span"]
    file_key: str
    ast_type: Slug | None = None
    """Syntax node type, using tree-sitter grammar names (e.g. ``import_from_statement``)."""

    _check_file_key = field_validator("file_key")(_file_key)


class ConfigEvidence(_EvidenceBase):
    """A value in a structured config file (JSON, TOML, YAML).

    ``json_pointer`` (RFC 6901) addresses the parsed data model, whatever the
    file's syntax: ``/project/dependencies/0`` in a pyproject.toml.
    """

    type: Literal["config"]
    file_key: str
    json_pointer: str

    _check_file_key = field_validator("file_key")(_file_key)

    @field_validator("json_pointer")
    @classmethod
    def _pointer(cls, value: str) -> str:
        if not _JSON_POINTER.fullmatch(value):
            raise ValueError(f"invalid JSON pointer: {value!r}")
        return value


class ConventionEvidence(_EvidenceBase):
    """A naming or layout convention matched, e.g. ``test_*.py`` on a file name.

    The convention's identity is ``provenance.rule`` (required); ``pattern`` is
    its human-readable form, ``matched_value`` what it matched.
    """

    type: Literal["convention"]
    pattern: Text
    matched_value: Text

    @model_validator(mode="after")
    def _needs_rule(self) -> ConventionEvidence:
        if self.provenance.rule is None:
            raise ValueError("convention evidence must name its rule")
        return self


class DerivedEvidence(_EvidenceBase):
    """Computed from other records of the same snapshot (rollups, inference)."""

    type: Literal["derived"]
    node_keys: tuple[str, ...] = ()
    edge_refs: tuple[EdgeRef, ...] = ()

    @model_validator(mode="after")
    def _non_empty(self) -> DerivedEvidence:
        if not self.node_keys and not self.edge_refs:
            raise ValueError("derived evidence must reference at least one node or edge")
        return self


class AiEvidence(CIMModel):
    """A claim made by a model. Allowed only in the interpretation tier.

    Snapshot and derivation records reject this type, so AI output cannot become
    a graph fact by being attached as evidence.
    """

    type: Literal["ai"]
    basis: Basis
    model: Text
    prompt_version: Text
    cited_evidence_refs: tuple[Citation, ...] = Field(min_length=1)

    @field_validator("basis")
    @classmethod
    def _interpreted(cls, value: Basis) -> Basis:
        if value is not Basis.INTERPRETED:
            raise ValueError("ai evidence always has basis 'interpreted'")
        return value


Evidence = Annotated[
    SpanEvidence | ConfigEvidence | ConventionEvidence | DerivedEvidence | AiEvidence,
    Field(discriminator="type"),
]

FactEvidence = Annotated[
    SpanEvidence | ConfigEvidence | ConventionEvidence | DerivedEvidence,
    Field(discriminator="type"),
]
"""Evidence acceptable in the snapshot and derivation tiers: anything but ``ai``."""


def strongest_basis(evidence: Sequence[_EvidenceBase]) -> Basis:
    return max((item.basis for item in evidence), key=lambda b: b.strength)
