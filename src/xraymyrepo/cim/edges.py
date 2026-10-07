"""Snapshot-tier edges.

Direction is always ``source -> target`` and reads as a sentence with the source
as the active party: ``file IMPORTS file``, ``function HANDLES endpoint``.
Inverse edges are never stored; impact analysis traverses in reverse.

An edge is unique per snapshot on ``(kind, source_key, target_key)``. Repeated
occurrences (two import statements for the same target) are one edge with
``occurrence_count`` and a capped evidence sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field, SerializeAsAny, field_validator, model_validator

from ._base import MAX_EVIDENCE_SAMPLE, CIMModel, Slug, Text
from .enums import (
    SNAPSHOT_BASES,
    Basis,
    CallKind,
    Confidence,
    EdgeKind,
    InheritanceMode,
    NodeKind,
    RequirementScope,
)
from .evidence import FactEvidence, SpanEvidence, strongest_basis
from .refs import EdgeRef

# ---------------------------------------------------------------------------
# Kind-specific attributes


class EdgeAttributes(CIMModel):
    pass


class ImportsAttributes(EdgeAttributes):
    specifiers: tuple[Text, ...] = Field(min_length=1)
    """Module specifiers as written, sorted and unique (``..db``, ``./orders``)."""
    imported_names: tuple[Text, ...] = ()
    """Names bound by the import(s), sorted and unique. ``default`` for JS default imports."""
    is_type_only: bool = False
    """True when every occurrence is a type-only import (TS ``import type``)."""
    is_reexport: bool = False
    """True when every occurrence re-exports (``export { x } from``, ``__init__`` re-exports)."""
    is_dynamic: bool = False
    """True when every occurrence is a dynamic import with a resolvable literal specifier."""
    via: tuple[str, ...] = ()
    """File keys of barrel/re-export files the import was resolved through, in order."""

    @field_validator("specifiers", "imported_names")
    @classmethod
    def _sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(set(value)):
            raise ValueError("must be sorted and unique")
        return value


class RequiresAttributes(EdgeAttributes):
    scope: RequirementScope
    version_constraint: Text | None = None


class InheritsAttributes(EdgeAttributes):
    mode: InheritanceMode
    target_symbol: Text | None = None
    """Qualified name inside an external package (``pydantic.BaseModel``)."""


class HandlesAttributes(EdgeAttributes):
    framework: Slug | None = None


class TestsAttributes(EdgeAttributes):
    __test__ = False  # not a pytest test class


class CallsAttributes(EdgeAttributes):
    call_kind: CallKind
    target_symbol: Text | None = None
    """Qualified name inside an external package, when the target is one."""


@dataclass(frozen=True)
class EdgeRule:
    attributes: type[EdgeAttributes]
    sources: frozenset[NodeKind]
    targets: frozenset[NodeKind]
    bases: frozenset[Basis]
    allows_self_loop: bool = False


_SYMBOL_OR_FILE = frozenset({NodeKind.FILE, NodeKind.TYPE, NodeKind.FUNCTION})

# Allowed bases are locked for v1 and mirrored by the SQL edge_kind_basis_check.
# IMPORTS, INHERITS and CALLS are never 'observed': seeing ``from .db import User``
# is an observed *source fact*, but a stored edge exists only once that reference
# has a concrete target (resolved, or heuristic when a rule picked it). Until then
# the reference is an ``UnresolvedReference``. TESTS is heuristic in v1; runtime
# coverage evidence could later justify observed TESTS edges.

EDGE_RULES: dict[EdgeKind, EdgeRule] = {
    EdgeKind.IMPORTS: EdgeRule(
        ImportsAttributes,
        sources=_SYMBOL_OR_FILE,  # innermost named scope containing the import
        targets=_SYMBOL_OR_FILE | {NodeKind.EXTERNAL_PACKAGE},  # most specific resolved node
        bases=frozenset({Basis.RESOLVED, Basis.HEURISTIC}),
    ),
    EdgeKind.REQUIRES: EdgeRule(
        RequiresAttributes,
        sources=frozenset({NodeKind.PACKAGE}),
        targets=frozenset({NodeKind.PACKAGE, NodeKind.EXTERNAL_PACKAGE}),
        bases=frozenset({Basis.OBSERVED}),
    ),
    EdgeKind.INHERITS: EdgeRule(
        InheritsAttributes,
        sources=frozenset({NodeKind.TYPE}),
        targets=frozenset({NodeKind.TYPE, NodeKind.EXTERNAL_PACKAGE}),
        bases=frozenset({Basis.RESOLVED, Basis.HEURISTIC}),
    ),
    EdgeKind.HANDLES: EdgeRule(
        HandlesAttributes,
        sources=frozenset({NodeKind.FUNCTION, NodeKind.TYPE}),
        targets=frozenset({NodeKind.ENDPOINT}),
        bases=frozenset({Basis.HEURISTIC}),
    ),
    EdgeKind.TESTS: EdgeRule(
        TestsAttributes,
        sources=_SYMBOL_OR_FILE,
        targets=_SYMBOL_OR_FILE,
        bases=frozenset({Basis.HEURISTIC}),
    ),
    EdgeKind.CALLS: EdgeRule(
        CallsAttributes,
        sources=frozenset({NodeKind.FILE, NodeKind.FUNCTION}),
        targets=frozenset({NodeKind.FUNCTION, NodeKind.TYPE, NodeKind.EXTERNAL_PACKAGE}),
        bases=frozenset({Basis.RESOLVED, Basis.HEURISTIC}),
        allows_self_loop=True,  # recursion
    ),
}


class Edge(CIMModel):
    kind: EdgeKind
    source_key: str
    target_key: str
    basis: Basis
    """The strongest basis among the evidence items."""
    confidence: Confidence
    occurrence_count: int = Field(default=1, ge=1)
    """Source occurrences of the relationship; span evidence items are a sample of them."""
    evidence: tuple[FactEvidence, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_SAMPLE)
    ambiguity_group: Slug | None = None
    """Shared by candidate edges for one reference that resolved to several targets."""
    contested: bool = False
    attributes: SerializeAsAny[EdgeAttributes]

    @model_validator(mode="before")
    @classmethod
    def _typed_attributes(cls, data: Any) -> Any:
        if isinstance(data, dict):
            try:
                kind = EdgeKind(str(data.get("kind")))
            except ValueError:
                return data
            attrs = data.get("attributes", {})
            if not isinstance(attrs, EdgeAttributes):
                data = {**data, "attributes": EDGE_RULES[kind].attributes.model_validate(attrs)}
        return data

    @field_validator("basis")
    @classmethod
    def _snapshot_basis(cls, basis: Basis) -> Basis:
        if basis not in SNAPSHOT_BASES:
            raise ValueError(f"snapshot edges cannot have basis '{basis}'")
        return basis

    @model_validator(mode="after")
    def _consistent(self) -> Edge:
        rule = EDGE_RULES[self.kind]
        if type(self.attributes) is not rule.attributes:
            raise ValueError(f"{self.kind} edges need {rule.attributes.__name__}")
        if self.basis not in rule.bases:
            raise ValueError(f"{self.kind} edges cannot have basis '{self.basis}'")
        if self.basis is not strongest_basis(self.evidence):
            raise ValueError("edge basis must equal the strongest basis of its evidence")
        if self.occurrence_count < sum(isinstance(e, SpanEvidence) for e in self.evidence):
            raise ValueError("occurrence_count cannot be smaller than the sampled spans")
        if self.source_key == self.target_key and not rule.allows_self_loop:
            raise ValueError(f"{self.kind} edges cannot be self-loops")
        if self.basis is Basis.OBSERVED and self.confidence is not Confidence.HIGH:
            raise ValueError("observed claims always have high confidence")
        if self.ambiguity_group is not None and self.confidence is not Confidence.LOW:
            raise ValueError("ambiguous candidate edges must have low confidence")
        return self

    @property
    def ref(self) -> EdgeRef:
        return EdgeRef.of(self.kind, self.source_key, self.target_key)
