"""Relationships (stored CIM edges) as seen from a node, and single edges."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from xraymyrepo.cim import (
    Basis,
    CallsAttributes,
    Confidence,
    EdgeKind,
    HandlesAttributes,
    ImportsAttributes,
    InheritsAttributes,
    RequiresAttributes,
    TestsAttributes,
)
from xraymyrepo.cim.evidence import FactEvidence

from .common import ApiModel, NodeBrief
from .nodes import NodeSummary

EdgeAttributes = (
    ImportsAttributes
    | RequiresAttributes
    | InheritsAttributes
    | HandlesAttributes
    | TestsAttributes
    | CallsAttributes
)
"""The CIM attribute model of the edge's kind."""


class _EdgeFields(ApiModel):
    kind: EdgeKind
    source_key: str
    target_key: str
    basis: Basis
    confidence: Confidence
    occurrence_count: int
    ambiguity_group: str | None
    contested: bool
    attributes: EdgeAttributes


class Relationship(_EdgeFields):
    """A stored edge touching the requested node, always in its stored direction
    (``source_key`` -> ``target_key``); inverse edges are never synthesized."""

    direction: Literal["out", "in"] = Field(
        description="`out`: the requested node is the source; `in`: it is the target."
    )
    peer: NodeBrief
    """The node at the other end."""


class EdgeDetail(_EdgeFields):
    source: NodeSummary
    target: NodeSummary
    evidence: list[FactEvidence]
