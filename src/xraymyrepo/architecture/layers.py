"""Layer inference for files in the layering scope (rule ``arch.layer.resolve``).

Signals, each cited as one ``derived`` evidence item with its own rule:

    arch.layer.framework_import  strong  the file depends on an external package from
                                         ``params.framework_signals`` (cites the edges)
    arch.layer.role              strong  the file or one of its symbols has a role from
                                         ``params.role_signals`` (cites the nodes)
    arch.layer.name              weak    the nearest of the file stem and its directory
                                         names found in ``params.name_signals``
    arch.layer.direction         graph   dependency edges to or from files whose strong
                                         layer allows (supports) or forbids (contradicts)
                                         the candidate layer

Resolution:
    * two different strong layers                -> unclassified (no record)
    * one strong layer W:
        name disagrees, or any contradicting edge -> W, low
        >= 2 signal kinds agree (framework, role,
        name)                                     -> W, high
        otherwise                                 -> W, medium
    * no strong layer, one name layer N:
        >= 1 supporting edge and none contradicting -> N, medium
        otherwise                                 -> unclassified: a name alone is a
                                                     convention, not an inference
    * nothing                                     -> unclassified

Coverage: every considered file gets ``layer_considered = 1`` and the repository
node gets ``layer_files_considered/classified/unclassified``, so "unclassified"
(considered, no layer) is distinguishable from "not considered".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xraymyrepo.cim import (
    REPOSITORY_KEY,
    Basis,
    Confidence,
    DerivedEvidence,
    EdgeRef,
    InferredLayer,
    NodeMetric,
)
from xraymyrepo.cim._base import MAX_EVIDENCE_SAMPLE
from xraymyrepo.cim.keys import decode_path, parent_path_key
from xraymyrepo.cim.nodes import Node
from xraymyrepo.cim.provenance import Provenance

from .index import FileEdge, SnapshotIndex
from .params import ALGORITHM

RULE_RESOLVE = "arch.layer.resolve"
RULE_FRAMEWORK = "arch.layer.framework_import"
RULE_ROLE = "arch.layer.role"
RULE_NAME = "arch.layer.name"
RULE_DIRECTION = "arch.layer.direction"
RULE_COVERAGE = "arch.layer.coverage"


@dataclass
class _Signals:
    framework: dict[str, list[tuple[EdgeRef, str]]] = field(default_factory=dict)
    role: dict[str, list[str]] = field(default_factory=dict)
    name: tuple[str, str] | None = None
    """(layer, the file or directory key whose name matched)."""

    @property
    def strong(self) -> set[str]:
        return set(self.framework) | set(self.role)


@dataclass(frozen=True)
class LayerResult:
    layers: dict[str, InferredLayer]
    metrics: list[NodeMetric]


def _evidence(rule: str, node_keys: tuple[str, ...] = (),
              edge_refs: tuple[EdgeRef, ...] = ()) -> DerivedEvidence:  # fmt: skip
    return DerivedEvidence(type="derived", basis=Basis.INFERRED,
                           provenance=Provenance(producer=ALGORITHM, rule=rule),
                           node_keys=node_keys, edge_refs=edge_refs)  # fmt: skip


def _signals(index: SnapshotIndex, file: Node) -> _Signals:
    p = index.params
    found = _Signals()
    for edge, external in index.external_dependencies.get(file.key, ()):
        layer = p.framework_signals.get(index.package_name(external))
        if layer is not None:
            found.framework.setdefault(layer, []).append((edge.ref, external.key))
    for role, node_key in index.roles.get(file.key, ()):
        layer = p.role_signals.get(role)
        if layer is not None:
            found.role.setdefault(layer, []).append(node_key)
    stem = decode_path(file.key).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    candidates = [(stem, file.key)]
    directory = parent_path_key(file.key)
    while directory != REPOSITORY_KEY:
        candidates.append((decode_path(directory).rsplit("/", 1)[-1], directory))
        directory = parent_path_key(directory)
    for name, key in candidates:
        layer = p.name_signals.get(name.lower())
        if layer is not None:
            found.name = (layer, key)
            break
    return found


def infer_layers(index: SnapshotIndex) -> LayerResult:
    params = index.params
    scope = {f.key: f for f in index.layer_scope}
    signals = {key: _signals(index, f) for key, f in scope.items()}
    strong = {k: next(iter(s.strong)) for k, s in signals.items() if len(s.strong) == 1}

    touching: dict[str, list[FileEdge]] = {}
    for fe in index.dependency_edges:
        if fe.source_file in scope and fe.target_file in scope:
            touching.setdefault(fe.source_file, []).append(fe)
            touching.setdefault(fe.target_file, []).append(fe)

    def direction(key: str, layer: str) -> tuple[list[EdgeRef], list[EdgeRef]]:
        supporting: list[EdgeRef] = []
        contradicting: list[EdgeRef] = []
        for fe in touching.get(key, ()):
            outgoing = fe.source_file == key
            peer = strong.get(fe.target_file if outgoing else fe.source_file)
            if peer is None:
                continue
            allowed = params.allows(layer, peer) if outgoing else params.allows(peer, layer)
            (supporting if allowed else contradicting).append(fe.edge.ref)
        return supporting, contradicting

    layers: dict[str, InferredLayer] = {}
    for key, s in signals.items():
        name_layer = s.name[0] if s.name else None
        if len(s.strong) > 1:
            continue
        layer = next(iter(s.strong)) if s.strong else name_layer
        if layer is None:
            continue
        supporting, contradicting = direction(key, layer)
        if s.strong:
            agreeing = bool(s.framework) + bool(s.role) + (name_layer == layer)
            if (name_layer is not None and name_layer != layer) or contradicting:
                confidence = Confidence.LOW
            elif agreeing >= 2:
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MEDIUM
        elif supporting and not contradicting:
            confidence = Confidence.MEDIUM
        else:
            continue  # a name alone stays unclassified

        evidence = []
        if s.framework:
            items = s.framework[layer]
            evidence.append(_evidence(RULE_FRAMEWORK,
                                      tuple(sorted({external for _, external in items})),
                                      tuple(ref for ref, _ in items)))  # fmt: skip
        if s.role:
            evidence.append(_evidence(RULE_ROLE, tuple(sorted(set(s.role[layer])))))
        if s.name is not None and name_layer == layer:
            evidence.append(_evidence(RULE_NAME, (s.name[1],)))
        if supporting:
            evidence.append(
                _evidence(RULE_DIRECTION, edge_refs=tuple(supporting[:MAX_EVIDENCE_SAMPLE]))
            )
        layers[key] = InferredLayer(
            node_key=key, layer=layer, basis=Basis.INFERRED, confidence=confidence,
            provenance=Provenance(producer=ALGORITHM, rule=RULE_RESOLVE),
            evidence=tuple(evidence),
        )  # fmt: skip

    coverage = Provenance(producer=ALGORITHM, rule=RULE_COVERAGE)
    metrics = [NodeMetric(node_key=key, metric="layer_considered", value=1.0, provenance=coverage)
               for key in sorted(scope)]  # fmt: skip
    for name, value in (("layer_files_considered", len(scope)),
                        ("layer_files_classified", len(layers)),
                        ("layer_files_unclassified", len(scope) - len(layers))):  # fmt: skip
        metrics.append(NodeMetric(node_key=REPOSITORY_KEY, metric=name, value=float(value),
                                  provenance=coverage))  # fmt: skip
    return LayerResult(layers, metrics)
