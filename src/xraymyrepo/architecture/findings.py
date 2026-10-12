"""Findings: ``arch.layer.violation`` and ``arch.group.cycle``.

* ``arch.layer.violation``: a dependency edge from a file of one inferred layer to a
  file of a layer it may not depend on (``params.allows``); one finding per file pair,
  citing every such edge (up to the evidence sample size). Its confidence is the lower
  of the two layers' confidences.
* ``arch.group.cycle``: groups of the components lens that depend on each other
  (a strongly connected set of size >= 2 over dependency group edges). Its confidence
  is the lowest confidence among the groups; the evidence cites the sample edges of
  each group edge inside the cycle.

Both are ``inferred``: they rest on inferred layers or inferred groups.
"""

from __future__ import annotations

from xraymyrepo.cim import (
    Basis,
    Confidence,
    DerivedEvidence,
    EdgeRef,
    Finding,
    FindingSeverity,
    GroupEdge,
    InferredLayer,
)
from xraymyrepo.cim._base import MAX_EVIDENCE_SAMPLE
from xraymyrepo.cim.provenance import Provenance
from xraymyrepo.graph import strongly_connected

from .index import SnapshotIndex
from .lenses import LensResult
from .params import ALGORITHM

RULE_VIOLATION = "arch.layer.violation"
RULE_CYCLE = "arch.group.cycle"

_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def _lowest(confidences: list[Confidence]) -> Confidence:
    return min(confidences, key=_RANK.__getitem__)


def _evidence(
    rule: str, node_keys: tuple[str, ...], edge_refs: tuple[EdgeRef, ...]
) -> DerivedEvidence:
    return DerivedEvidence(type="derived", basis=Basis.INFERRED,
                           provenance=Provenance(producer=ALGORITHM, rule=rule),
                           node_keys=node_keys, edge_refs=edge_refs)  # fmt: skip


def layer_violations(index: SnapshotIndex, layers: dict[str, InferredLayer]) -> list[Finding]:
    pairs: dict[tuple[str, str], list[EdgeRef]] = {}
    for fe in index.dependency_edges:
        source, target = layers.get(fe.source_file), layers.get(fe.target_file)
        if source is None or target is None:
            continue
        if not index.params.allows(source.layer, target.layer):
            pairs.setdefault((fe.source_file, fe.target_file), []).append(fe.edge.ref)
    findings = []
    for (source_key, target_key), refs in sorted(pairs.items()):
        source, target = layers[source_key], layers[target_key]
        findings.append(Finding(
            rule=RULE_VIOLATION,
            severity=FindingSeverity.MEDIUM,
            subject_keys=(source_key, target_key),
            message=(f"{source_key} ({source.layer}) depends on {target_key} ({target.layer}); "
                     f"{source.layer} may not depend on {target.layer}"),
            basis=Basis.INFERRED,
            confidence=_lowest([source.confidence, target.confidence]),
            evidence=(_evidence(RULE_VIOLATION, (source_key, target_key),
                                tuple(refs[:MAX_EVIDENCE_SAMPLE])),),
        ))  # fmt: skip
    return findings


def group_cycles(index: SnapshotIndex, lens: LensResult, edges: list[GroupEdge]) -> list[Finding]:
    dependency = set(index.params.dependency_kinds)
    links: dict[tuple[str, str], list[GroupEdge]] = {}
    for edge in edges:
        if edge.edge_kind in dependency:
            links.setdefault((edge.source_group_key, edge.target_group_key), []).append(edge)
    adjacency: dict[str, list[str]] = {}
    for source, target in sorted(links):
        adjacency.setdefault(source, []).append(target)
    findings = []
    for component in sorted(strongly_connected(sorted(lens.groups), adjacency)):
        if len(component) < 2:
            continue
        members = set(component)
        inside = [e for (s, t), group_edges in sorted(links.items())
                  if s in members and t in members for e in group_edges]  # fmt: skip
        evidence = tuple(
            _evidence(RULE_CYCLE, (), e.sample_edge_refs) for e in inside[:MAX_EVIDENCE_SAMPLE]
        )
        labels = ", ".join(lens.groups[k].label or k for k in component)
        findings.append(Finding(
            rule=RULE_CYCLE,
            severity=FindingSeverity.MEDIUM,
            subject_keys=tuple(component),
            message=f"Groups {labels} depend on each other (dependency cycle)",
            basis=Basis.INFERRED,
            confidence=_lowest([lens.groups[k].confidence for k in component]),
            evidence=evidence,
        ))  # fmt: skip
    return findings
