"""Checks of a derivation against the snapshot it was derived from (any algorithm).

``Derivation`` checks itself (lenses, group trees, memberships, bases). What it
cannot check without the snapshot is checked here: every key it cites exists, and
every record names the derivation's own algorithm as producer.
"""

from __future__ import annotations

from collections.abc import Iterable

from xraymyrepo.cim import Derivation, DerivedEvidence, NodeKind, SnapshotDocument
from xraymyrepo.cim.evidence import FactEvidence


class DerivationContractError(ValueError):
    """A derivation that does not fit its snapshot: a derivation bug, never repaired."""


def validate_against(doc: SnapshotDocument, derivation: Derivation) -> None:
    nodes = doc.nodes_by_key
    edges = doc.edges_by_ref
    groups = {g.key for g in derivation.groups}
    producer = derivation.run.algorithm

    def fail(message: str) -> None:
        raise DerivationContractError(message)

    def node(key: str, what: str) -> None:
        if key not in nodes:
            fail(f"{what}: unknown node {key!r}")

    def cited(evidence: Iterable[FactEvidence], what: str) -> None:
        for item in evidence:
            if item.provenance.producer != producer:
                fail(f"{what}: evidence produced by {item.provenance.producer!r}")
            if isinstance(item, DerivedEvidence):
                for key in item.node_keys:
                    node(key, f"{what} evidence")
                for ref in item.edge_refs:
                    if (ref.kind, ref.source_key, ref.target_key) not in edges:
                        fail(f"{what} evidence: unknown edge {ref.kind} {ref.source_key!r} -> "
                             f"{ref.target_key!r}")  # fmt: skip

    if derivation.run.snapshot != doc.snapshot:
        fail("the derivation run names another snapshot")
    for group in derivation.groups:
        if group.provenance.producer != producer:
            fail(f"group {group.key!r} produced by {group.provenance.producer!r}")
        cited(group.evidence, f"group {group.key!r}")
    for member in derivation.members:
        node(member.node_key, f"member of {member.group_key!r}")
        if nodes[member.node_key].kind is not NodeKind.FILE:
            fail(f"member {member.node_key!r} is not a file")
    for group_edge in derivation.group_edges:
        for ref in group_edge.sample_edge_refs:
            if (ref.kind, ref.source_key, ref.target_key) not in edges:
                fail(f"group edge sample: unknown edge {ref.source_key!r} -> {ref.target_key!r}")
    for metric in derivation.metrics:
        if metric.node_key not in groups:
            node(metric.node_key, f"metric {metric.metric}")
        if metric.provenance.producer != producer:
            fail(f"metric {metric.metric} produced by {metric.provenance.producer!r}")
    seen_metrics = [(m.node_key, m.metric) for m in derivation.metrics]
    if len(set(seen_metrics)) != len(seen_metrics):
        fail("a metric is recorded twice for one subject")
    for finding in derivation.findings:
        for key in finding.subject_keys:
            if key not in groups:
                node(key, f"finding {finding.rule}")
        cited(finding.evidence, f"finding {finding.rule}")
    for layer in derivation.layers:
        node(layer.node_key, "inferred layer")
        if layer.provenance.producer != producer:
            fail(f"layer of {layer.node_key!r} produced by {layer.provenance.producer!r}")
        cited(layer.evidence, f"layer of {layer.node_key!r}")
