"""Derivation tier: projections and computations over one snapshot.

A derivation run is immutable and identified by
``(snapshot, algorithm, algorithm_version, params_hash)``, so groupings and
metrics can be recomputed without re-extracting facts.

Groups belong to a *lens*. Each lens is one tree of groups over the snapshot's
files (``directory``, ``package``, ``inferred``). Inferred lenses carry basis
``inferred`` on every group and membership, so they can always be shown as
inferred. Nothing here may have basis ``interpreted``: AI-chosen names are
annotations (``interpretation.py``), never group labels.

Layers computed from the graph (``InferredLayer``) live here too, always with
basis ``inferred``. The snapshot only holds heuristic ``layer`` classifications
(a convention matched), so an inferred layer can never pass for a snapshot fact.

Contract only: no lens is computed in CIM v1.
"""

from __future__ import annotations

import math

from pydantic import Field, field_validator, model_validator

from ._base import MAX_EVIDENCE_SAMPLE, CIMModel, RuleId, SemVer, Sha256, Slug, Text
from .enums import (
    DERIVATION_BASES,
    Basis,
    Confidence,
    EdgeKind,
    FindingSeverity,
    LensKind,
    NodeKind,
    SnapshotStatus,
)
from .evidence import DerivedEvidence, FactEvidence
from .keys import is_prefixed, parse_prefixed_key, parse_symbol_key, validate_key
from .provenance import Provenance
from .refs import EdgeRef
from .snapshot import SnapshotIdentity


def _derivation_basis(basis: Basis) -> Basis:
    if basis not in DERIVATION_BASES:
        raise ValueError(f"derivation records cannot have basis '{basis}'")
    return basis


def lens_of(group_key: str) -> str:
    return parse_prefixed_key(group_key, "group").namespace


class DerivationRun(CIMModel):
    snapshot: SnapshotIdentity
    algorithm: Slug
    algorithm_version: SemVer
    params_hash: Sha256
    status: SnapshotStatus = SnapshotStatus.PENDING


class Lens(CIMModel):
    name: Slug
    kind: LensKind


class Group(CIMModel):
    key: str
    parent_key: str | None = None
    label: Text | None = None
    """Deterministic label (a directory or package name). Never AI-generated."""
    basis: Basis
    confidence: Confidence
    provenance: Provenance
    evidence: tuple[FactEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE_SAMPLE)

    _basis = field_validator("basis")(_derivation_basis)

    @model_validator(mode="after")
    def _consistent(self) -> Group:
        validate_key(NodeKind.GROUP, self.key)
        if self.parent_key is not None:
            validate_key(NodeKind.GROUP, self.parent_key)
            if lens_of(self.parent_key) != self.lens:
                raise ValueError("a group's parent must be in the same lens")
        return self

    @property
    def lens(self) -> str:
        return lens_of(self.key)


class GroupMember(CIMModel):
    """Assigns a file to a group. Symbols belong to the group of their file."""

    group_key: str
    node_key: str
    basis: Basis
    confidence: Confidence

    _basis = field_validator("basis")(_derivation_basis)

    @model_validator(mode="after")
    def _consistent(self) -> GroupMember:
        validate_key(NodeKind.GROUP, self.group_key)
        validate_key(NodeKind.FILE, self.node_key)
        return self


class GroupEdge(CIMModel):
    """Rollup of leaf edges of one kind between two groups of the same lens.

    Never averages confidence: ``basis_counts`` keeps how many underlying edges
    have each basis, so a renderer can show "40 resolved, 2 heuristic".
    """

    source_group_key: str
    target_group_key: str
    edge_kind: EdgeKind
    count: int = Field(ge=1)
    basis_counts: dict[Basis, int]
    sample_edge_refs: tuple[EdgeRef, ...] = Field(default=(), max_length=MAX_EVIDENCE_SAMPLE)

    @model_validator(mode="after")
    def _consistent(self) -> GroupEdge:
        for key in (self.source_group_key, self.target_group_key):
            validate_key(NodeKind.GROUP, key)
        if self.source_group_key == self.target_group_key:
            raise ValueError("edges inside one group are not rolled up")
        if lens_of(self.source_group_key) != lens_of(self.target_group_key):
            raise ValueError("rolled-up edges connect groups of the same lens")
        if any(b not in DERIVATION_BASES or n < 1 for b, n in self.basis_counts.items()):
            raise ValueError("basis_counts must hold positive counts of derivation bases")
        if sum(self.basis_counts.values()) != self.count:
            raise ValueError("basis_counts must sum to count")
        if any(ref.kind is not self.edge_kind for ref in self.sample_edge_refs):
            raise ValueError("sample edges must be of the rolled-up kind")
        return self


class NodeMetric(CIMModel):
    node_key: str
    metric: Slug
    value: float
    provenance: Provenance

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric values must be finite")
        return value


class Finding(CIMModel):
    """A code health or risk observation about one or more nodes."""

    rule: RuleId
    severity: FindingSeverity
    subject_keys: tuple[str, ...] = Field(min_length=1)
    message: Text
    basis: Basis
    confidence: Confidence
    evidence: tuple[FactEvidence, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_SAMPLE)

    _basis = field_validator("basis")(_derivation_basis)


class InferredLayer(CIMModel):
    """An architectural layer computed for a node by a layering algorithm.

    Always basis ``inferred`` and always produced by a named rule. Its evidence is
    ``derived`` evidence citing the nodes and edges the inference used. A layer
    asserted by a convention is a snapshot ``layer`` classification instead.
    """

    node_key: str
    """A directory, file, type or function key (the kinds a layer applies to)."""
    layer: Slug
    basis: Basis
    confidence: Confidence
    provenance: Provenance
    evidence: tuple[DerivedEvidence, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_SAMPLE)

    @model_validator(mode="after")
    def _consistent(self) -> InferredLayer:
        if self.basis is not Basis.INFERRED:
            raise ValueError("inferred layers must have basis 'inferred'")
        if self.provenance.rule is None:
            raise ValueError("inferred layers must name the rule that produced them")
        if any(e.basis is not Basis.INFERRED for e in self.evidence):
            raise ValueError("inferred layer evidence must have basis 'inferred'")
        if is_prefixed(self.node_key):
            raise ValueError(f"layers apply to directories, files and symbols: {self.node_key!r}")
        if "#" in self.node_key:
            parse_symbol_key(self.node_key)
        else:
            validate_key(NodeKind.FILE, self.node_key)
        return self


class Derivation(CIMModel):
    """One derivation run and everything it produced, with cross-record checks."""

    run: DerivationRun
    lenses: tuple[Lens, ...] = ()
    groups: tuple[Group, ...] = ()
    members: tuple[GroupMember, ...] = ()
    group_edges: tuple[GroupEdge, ...] = ()
    metrics: tuple[NodeMetric, ...] = ()
    findings: tuple[Finding, ...] = ()
    layers: tuple[InferredLayer, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Derivation:
        lenses = {lens.name: lens for lens in self.lenses}
        if len(lenses) != len(self.lenses):
            raise ValueError("duplicate lens name")
        groups = {group.key: group for group in self.groups}
        if len(groups) != len(self.groups):
            raise ValueError("duplicate group key")

        def inferred(group_key: str) -> bool:
            return lenses[lens_of(group_key)].kind is LensKind.INFERRED

        for group in self.groups:
            if group.lens not in lenses:
                raise ValueError(f"group {group.key!r} uses undeclared lens {group.lens!r}")
            if group.parent_key is not None and group.parent_key not in groups:
                raise ValueError(f"group {group.key!r} has unknown parent {group.parent_key!r}")
            if inferred(group.key) != (group.basis is Basis.INFERRED):
                raise ValueError(
                    f"group {group.key!r}: basis must be 'inferred' exactly when "
                    "its lens is inferred"
                )
        _check_acyclic(groups)

        assigned: set[tuple[str, str]] = set()
        for member in self.members:
            if member.group_key not in groups:
                raise ValueError(f"membership references unknown group {member.group_key!r}")
            if inferred(member.group_key) != (member.basis is Basis.INFERRED):
                raise ValueError("membership basis must be 'inferred' exactly in inferred lenses")
            slot = (lens_of(member.group_key), member.node_key)
            if slot in assigned:
                raise ValueError(f"{member.node_key!r} is in two groups of lens {slot[0]!r}")
            assigned.add(slot)

        for group_edge in self.group_edges:
            for key in (group_edge.source_group_key, group_edge.target_group_key):
                if key not in groups:
                    raise ValueError(f"group edge references unknown group {key!r}")

        layered = [layer.node_key for layer in self.layers]
        if len(set(layered)) != len(layered):
            raise ValueError("a node has at most one inferred layer per derivation run")
        return self


def _check_acyclic(groups: dict[str, Group]) -> None:
    for start in groups:
        seen = {start}
        parent = groups[start].parent_key
        while parent is not None:
            if parent in seen:
                raise ValueError(f"group hierarchy has a cycle through {parent!r}")
            seen.add(parent)
            parent = groups[parent].parent_key if parent in groups else None
