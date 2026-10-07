"""Classifications: roles and facets attached to existing nodes.

Concepts such as "test", "data model", "config file" or "generated code" are not
node kinds. They are classifications of a file, type or function, each with its
own basis, confidence and evidence, so a guess about a role never changes what a
node *is*.

Facets:
    role    closed vocabulary (``Role``), restricted per node kind
    origin  closed vocabulary (``Origin``); every file has one (see document rules)
    layer   open slug vocabulary (``domain``, ``api``), assigned by convention rules

A snapshot ``layer`` is always *heuristic* evidence: a naming, layout or config
rule matched (``*/services/*`` => ``domain``). It is never computed over the graph:
a ``layer`` classification with ``derived`` evidence is rejected. Layers that an
algorithm infers from the dependency structure belong to a derivation run
(``derivation.InferredLayer``, basis ``inferred``), so they can never pass for a
snapshot fact.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from ._base import MAX_EVIDENCE_SAMPLE, CIMModel, is_slug
from .enums import SNAPSHOT_BASES, Basis, ClassificationFacet, Confidence, NodeKind, Origin, Role
from .evidence import DerivedEvidence, FactEvidence, strongest_basis

_ROLE_KINDS: dict[Role, frozenset[NodeKind]] = {
    Role.TEST_FILE: frozenset({NodeKind.FILE}),
    Role.TEST_FUNCTION: frozenset({NodeKind.FUNCTION}),
    Role.ORM_ENTITY: frozenset({NodeKind.TYPE}),
    Role.SCHEMA: frozenset({NodeKind.TYPE}),
    Role.CONFIG: frozenset({NodeKind.FILE}),
    Role.ENTRYPOINT: frozenset({NodeKind.FILE, NodeKind.FUNCTION}),
    Role.MANIFEST: frozenset({NodeKind.FILE}),
}
_ORIGIN_KINDS = frozenset({NodeKind.DIRECTORY, NodeKind.FILE})
_LAYER_KINDS = frozenset({NodeKind.DIRECTORY, NodeKind.FILE, NodeKind.TYPE, NodeKind.FUNCTION})


def applicable_kinds(facet: ClassificationFacet, value: str) -> frozenset[NodeKind]:
    match facet:
        case ClassificationFacet.ROLE:
            return _ROLE_KINDS[Role(value)]
        case ClassificationFacet.ORIGIN:
            return _ORIGIN_KINDS
        case ClassificationFacet.LAYER:
            return _LAYER_KINDS


class Classification(CIMModel):
    node_key: str
    facet: ClassificationFacet
    value: str
    basis: Basis
    confidence: Confidence
    contested: bool = False
    """Set when producers disagree, e.g. two different origins for one file."""
    evidence: tuple[FactEvidence, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_SAMPLE)

    @model_validator(mode="after")
    def _consistent(self) -> Classification:
        match self.facet:
            case ClassificationFacet.ROLE:
                Role(self.value)
            case ClassificationFacet.ORIGIN:
                Origin(self.value)
            case ClassificationFacet.LAYER:
                if not is_slug(self.value):
                    raise ValueError(f"layer must be a slug: {self.value!r}")
                if self.basis is not Basis.HEURISTIC:
                    raise ValueError(
                        "snapshot layer classifications are heuristic; inferred layers "
                        "belong to a derivation run"
                    )
                if any(isinstance(e, DerivedEvidence) for e in self.evidence):
                    raise ValueError(
                        "snapshot layer classifications cannot rest on derived (graph-computed) "
                        "evidence; inferred layers belong to a derivation run"
                    )
        if self.basis not in SNAPSHOT_BASES:
            raise ValueError(f"snapshot classifications cannot have basis '{self.basis}'")
        if self.basis is not strongest_basis(self.evidence):
            raise ValueError("classification basis must equal the strongest evidence basis")
        if self.basis is Basis.OBSERVED and self.confidence is not Confidence.HIGH:
            raise ValueError("observed claims always have high confidence")
        return self
