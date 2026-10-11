"""Group-to-group edges and group metrics of one lens.

Every snapshot edge whose two ends lie in two different files, both assigned in
the lens, is a *leaf edge*. Walking both files' group chains down from the lens
roots, the leaf edge contributes to ``GroupEdge(source group, target group, kind)``
at every level where the two chains differ (at most once per level); where they
agree, it is internal to that group. So the edges between the children of a group
X are exactly the group edges whose two ends are children of X.

Metrics (``arch.metrics.group``) count only dependency kinds (``params.dependency_kinds``):
``files`` (member files, including descendant groups), ``internal_edges``,
``afferent`` / ``efferent`` (leaf dependency edges entering / leaving the group) and
``instability`` = efferent / (afferent + efferent), omitted when both are 0.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from xraymyrepo.cim import Basis, EdgeKind, EdgeRef, GroupEdge, NodeMetric
from xraymyrepo.cim._base import MAX_EVIDENCE_SAMPLE
from xraymyrepo.cim.provenance import Provenance

from .index import SnapshotIndex
from .lenses import LensResult
from .params import ALGORITHM

RULE_METRICS = "arch.metrics.group"


@dataclass
class _Rollup:
    count: int = 0
    basis_counts: Counter[Basis] = field(default_factory=Counter)
    samples: list[EdgeRef] = field(default_factory=list)


def rollup(index: SnapshotIndex, lens: LensResult) -> tuple[list[GroupEdge], list[NodeMetric]]:
    dependency = set(index.params.dependency_kinds)
    rollups: dict[tuple[str, str, EdgeKind], _Rollup] = {}
    internal: Counter[str] = Counter()
    afferent: Counter[str] = Counter()
    efferent: Counter[str] = Counter()

    for file_edge in index.file_edges:
        source = lens.chain(file_edge.source_file)
        target = lens.chain(file_edge.target_file)
        if source is None or target is None:
            continue
        edge = file_edge.edge
        counted = edge.kind in dependency
        for s, t in zip(source, target, strict=False):
            if s == t:
                if counted:
                    internal[s] += 1
                continue
            item = rollups.setdefault((s, t, edge.kind), _Rollup())
            item.count += 1
            item.basis_counts[edge.basis] += 1
            if len(item.samples) < MAX_EVIDENCE_SAMPLE:
                item.samples.append(edge.ref)
            if counted:
                efferent[s] += 1
                afferent[t] += 1

    edges = [
        GroupEdge(
            source_group_key=s, target_group_key=t, edge_kind=kind, count=item.count,
            basis_counts=dict(sorted(item.basis_counts.items())),
            sample_edge_refs=tuple(item.samples),
        )
        for (s, t, kind), item in sorted(rollups.items())
    ]  # fmt: skip

    files: Counter[str] = Counter()
    for file_key in lens.members:
        for group in lens.chain(file_key) or ():
            files[group] += 1
    provenance = Provenance(producer=ALGORITHM, rule=RULE_METRICS)
    metrics = []
    for group in sorted(lens.groups):
        values: dict[str, float] = {
            "files": files[group],
            "internal_edges": internal[group],
            "afferent": afferent[group],
            "efferent": efferent[group],
        }
        if afferent[group] + efferent[group]:
            values["instability"] = efferent[group] / (afferent[group] + efferent[group])
        metrics += [
            NodeMetric(node_key=group, metric=name, value=float(value), provenance=provenance)
            for name, value in values.items()
        ]
    return edges, metrics
