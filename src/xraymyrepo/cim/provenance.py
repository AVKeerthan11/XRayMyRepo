"""Provenance: which producer, and which of its rules, made a claim."""

from __future__ import annotations

from ._base import CIMModel, RuleId, SemVer, Sha256, Slug


class Producer(CIMModel):
    """An extractor or algorithm at a specific version and configuration.

    Identity is ``(name, version, config_hash)``. Within one snapshot a producer
    name appears at most once, so records refer to producers by ``name``.
    """

    name: Slug
    version: SemVer
    config_hash: Sha256


class Provenance(CIMModel):
    """Who asserted a claim, and by which rule.

    ``rule`` is what answers "why does this exist?". It is mandatory for
    heuristic and inferred claims and for convention evidence (enforced by the
    evidence and record models), and recommended everywhere else.
    """

    producer: Slug
    rule: RuleId | None = None
