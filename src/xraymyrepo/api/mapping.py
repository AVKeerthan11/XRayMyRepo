"""CIM and persistence records -> API models. The only place the two vocabularies meet.

The mappers are pure; ``summaries`` and ``group_summaries`` additionally fetch what a
page of summaries needs in a fixed number of batched queries, whatever the page size.
"""

from __future__ import annotations

from collections.abc import Sequence

from xraymyrepo.cim import (
    ChangeRiskSummary,
    Classification,
    ClassificationFacet,
    ComparisonSummary,
    Confidence,
    Edge,
    EdgeChange,
    EdgeKind,
    Finding,
    Group,
    GroupEdge,
    InferredLayer,
    NodeChange,
    RiskItem,
)
from xraymyrepo.cim.nodes import Node
from xraymyrepo.cim.provenance import Provenance
from xraymyrepo.persistence import change_risk_queries as crq
from xraymyrepo.persistence import comparison_queries as cq
from xraymyrepo.persistence import derivation_queries as dq
from xraymyrepo.persistence import queries
from xraymyrepo.persistence.queries import NodeBrief as NodeBriefRecord

from . import models
from .db import Connection


def snapshot_summary(record: queries.SnapshotRecord) -> models.SnapshotSummary:
    identity = record.identity
    return models.SnapshotSummary(
        id=record.id,
        repository=identity.repository,
        commit_sha=identity.commit_sha,
        extractor_version=identity.extractor_version,
        config_hash=identity.config_hash,
        cim_schema_version=record.cim_schema_version,
        status=record.status,
        failure_reason=record.failure_reason,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )


def repository(record: queries.RepositoryRecord) -> models.Repository:
    ref = record.repository
    return models.Repository(
        host=ref.host,
        owner=ref.owner,
        name=ref.name,
        slug=ref.slug,
        snapshot_count=record.snapshot_count,
        latest_snapshot=(
            None if record.latest_snapshot is None else snapshot_summary(record.latest_snapshot)
        ),
    )


def snapshot_stats(stats: queries.SnapshotStats) -> models.SnapshotStats:
    return models.SnapshotStats(
        nodes_by_kind=stats.nodes_by_kind,
        edges_by_kind=stats.edges_by_kind,
        unresolved_by_reason=stats.unresolved_by_reason,
        issues_by_severity=stats.issues_by_severity,
    )


def node_brief(brief: NodeBriefRecord) -> models.NodeBrief:
    return models.NodeBrief(key=brief.key, kind=brief.kind, name=brief.name)


def node_summary(
    node: Node, child_count: int, tags: Sequence[tuple[ClassificationFacet, str]]
) -> models.NodeSummary:
    return models.NodeSummary(
        key=node.key,
        kind=node.kind,
        name=node.name,
        parent_key=node.parent_key,
        language=node.language,
        extraction_status=node.extraction_status,
        child_count=child_count,
        tags=[models.Tag(facet=facet, value=value) for facet, value in tags],
    )


def summaries(
    conn: Connection, snapshot_id: int, nodes: Sequence[Node]
) -> list[models.NodeSummary]:
    keys = [n.key for n in nodes]
    if not keys:
        return []
    counts = queries.count_children(conn, snapshot_id, keys)
    tags = queries.get_tags(conn, snapshot_id, keys)
    return [node_summary(n, counts.get(n.key, 0), tags.get(n.key, ())) for n in nodes]


def classification(c: Classification) -> models.ClassificationDetail:
    return models.ClassificationDetail(
        facet=c.facet, value=c.value, basis=c.basis, confidence=c.confidence,
        contested=c.contested, evidence=list(c.evidence),
    )  # fmt: skip


def node_detail(
    node: Node,
    *,
    child_count: int,
    classifications: Sequence[Classification],
    ancestors: Sequence[NodeBriefRecord],
    edge_counts: dict[EdgeKind, tuple[int, int]],
    diagnostic_counts: tuple[int, int],
) -> models.NodeDetail:
    summary = node_summary(node, child_count, [(c.facet, c.value) for c in classifications])
    return models.NodeDetail(
        **dict(summary),
        span=node.span,
        basis=node.basis,
        confidence=node.confidence,
        provenance=node.provenance,
        blob_sha=node.blob_sha,
        content_hash=node.content_hash,
        signature_hash=node.signature_hash,
        attributes=node.attributes,
        evidence=list(node.evidence),
        classifications=[classification(c) for c in classifications],
        ancestors=[node_brief(a) for a in ancestors],
        relationship_counts={
            kind: models.EdgeCount(outgoing=out, incoming=in_)
            for kind, (out, in_) in edge_counts.items()
        },
        diagnostic_counts=models.DiagnosticCounts(
            unresolved_references=diagnostic_counts[0], extraction_issues=diagnostic_counts[1]
        ),
    )


def relationship(item: queries.NodeEdge) -> models.Relationship:
    e = item.edge
    return models.Relationship(
        **_edge_fields(e), direction=item.direction.value, peer=node_brief(item.peer)
    )


def edge_detail(
    edge: Edge, source: models.NodeSummary, target: models.NodeSummary
) -> models.EdgeDetail:
    return models.EdgeDetail(
        **_edge_fields(edge), source=source, target=target, evidence=list(edge.evidence)
    )


def _edge_fields(e: Edge) -> dict[str, object]:
    return {
        "kind": e.kind,
        "source_key": e.source_key,
        "target_key": e.target_key,
        "basis": e.basis,
        "confidence": e.confidence,
        "occurrence_count": e.occurrence_count,
        "ambiguity_group": e.ambiguity_group,
        "contested": e.contested,
        "attributes": e.attributes,
    }


# ---------------------------------------------------------------------------
# Architecture intelligence (derivation runs)


def run_summary(record: dq.RunRecord) -> models.DerivationRunSummary:
    return models.DerivationRunSummary(
        id=record.id,
        snapshot_id=record.snapshot_id,
        algorithm=record.algorithm,
        algorithm_version=record.algorithm_version,
        params_hash=record.params_hash,
        status=record.status,
        failure_reason=record.failure_reason,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )


def lens_summary(lens: dq.LensRecord) -> models.LensSummary:
    return models.LensSummary(
        name=lens.name, kind=lens.kind, group_count=lens.groups, member_count=lens.members
    )


def layer_coverage(coverage: dq.LayerCoverage) -> models.LayerCoverage:
    return models.LayerCoverage(
        considered=coverage.considered,
        classified=coverage.classified,
        unclassified=coverage.unclassified,
        by_layer=coverage.by_layer,
        by_confidence=coverage.by_confidence,
    )


def group_summary(
    group: Group, counts: tuple[int, int], metrics: dict[str, float], mix: dq.LayerMix
) -> models.GroupSummary:
    return models.GroupSummary(
        key=group.key,
        lens=group.lens,
        label=group.label,
        parent_key=group.parent_key,
        basis=group.basis,
        confidence=group.confidence,
        child_group_count=counts[0],
        member_count=counts[1],
        metrics=metrics,
        layer_mix=models.LayerMix(by_layer=mix.by_layer, unclassified=mix.unclassified),
    )


def group_summaries(
    conn: Connection, run_id: int, groups: Sequence[Group]
) -> list[models.GroupSummary]:
    keys = [g.key for g in groups]
    if not keys:
        return []
    counts = dq.group_counts(conn, run_id, keys)
    metrics = dq.group_metrics(conn, run_id, keys)
    mixes = dq.layer_mix(conn, run_id, keys)
    return [group_summary(g, counts[g.key], metrics.get(g.key, {}), mixes[g.key]) for g in groups]


def group_detail(
    summary: models.GroupSummary, group: Group, ancestors: Sequence[dq.GroupBrief]
) -> models.GroupDetail:
    return models.GroupDetail(
        **dict(summary),
        provenance=group.provenance,
        evidence=list(group.evidence),
        ancestors=[models.GroupBrief(key=a.key, label=a.label) for a in ancestors],
    )


def group_edge_view(edge: GroupEdge) -> models.GroupEdgeView:
    return models.GroupEdgeView(
        source_group_key=edge.source_group_key,
        target_group_key=edge.target_group_key,
        edge_kind=edge.edge_kind,
        count=edge.count,
        basis_counts=edge.basis_counts,
        sample_edge_refs=list(edge.sample_edge_refs),
    )


def layer_assignment(layer: InferredLayer) -> models.LayerAssignment:
    return models.LayerAssignment(
        node_key=layer.node_key,
        layer=layer.layer,
        basis=layer.basis,
        confidence=layer.confidence,
        provenance=layer.provenance,
        evidence=list(layer.evidence),
    )


def finding_view(finding_id: int, finding: Finding) -> models.FindingView:
    return models.FindingView(
        id=finding_id,
        rule=finding.rule,
        severity=finding.severity,
        subject_keys=list(finding.subject_keys),
        message=finding.message,
        basis=finding.basis,
        confidence=finding.confidence,
        evidence=list(finding.evidence),
    )


def metric_view(record: dq.MetricRecord) -> models.MetricView:
    return models.MetricView(
        subject_key=record.subject_key,
        metric=record.metric,
        value=record.value,
        provenance=Provenance(producer=record.producer, rule=record.rule),
    )


def comparison_summary(record: cq.ComparisonRecord) -> models.ComparisonRunSummary:
    return models.ComparisonRunSummary(
        id=record.id,
        from_snapshot_id=record.from_snapshot_id,
        to_snapshot_id=record.to_snapshot_id,
        algorithm=record.algorithm,
        algorithm_version=record.algorithm_version,
        params_hash=record.params_hash,
        incomparable=list(record.incomparable),
        status=record.status,
        failure_reason=record.failure_reason,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )


def change_counts(summary: ComparisonSummary) -> models.ChangeCounts:
    return models.ChangeCounts(nodes=dict(summary.nodes), edges=dict(summary.edges))


def node_change_view(change: NodeChange) -> models.NodeChangeView:
    return models.NodeChangeView(
        key=change.key,
        change=change.change,
        kind=change.kind,
        from_key=change.from_key,
        to_key=change.to_key,
        match_method=change.match_method,
        confidence=change.confidence,
        move_ambiguous=change.move_ambiguous,
    )


def edge_change_view(change: EdgeChange) -> models.EdgeChangeView:
    return models.EdgeChangeView(
        change=change.change,
        kind=change.kind,
        from_ref=change.from_ref,
        to_ref=change.to_ref,
        confidence=change.confidence,
    )


def change_risk_summary(record: crq.ChangeRiskRecord) -> models.ChangeRiskRunSummary:
    complete = record.confidence is not None
    return models.ChangeRiskRunSummary(
        id=record.id,
        comparison_id=record.comparison_id,
        algorithm=record.algorithm,
        algorithm_version=record.algorithm_version,
        params_hash=record.params_hash,
        medium_at=record.medium_at,
        high_at=record.high_at,
        caveats=list(record.caveats),
        excluded_by_origin=record.excluded_by_origin,
        level=record.level,
        confidence=record.confidence,
        # Derived, as ChangeRiskAssessment.conclusive: never stored.
        conclusive=(
            record.confidence is Confidence.HIGH and not record.caveats and not record.capped_items
        )
        if complete
        else None,
        status=record.status,
        failure_reason=record.failure_reason,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )


def change_risk_counts(summary: ChangeRiskSummary) -> models.ChangeRiskCounts:
    return models.ChangeRiskCounts(
        levels=dict(summary.levels), confidences=dict(summary.confidences)
    )


def _risk_item_fields(item: RiskItem) -> dict[str, object]:
    return {
        "subject_key": item.subject_key,
        "change": item.change,
        "level": item.level,
        "points": item.points,
        "confidence": item.confidence,
        "capped": item.capped,
        "conclusive": item.conclusive,
        "caveats": list(item.caveats),
    }


def risk_item_summary(item: RiskItem) -> models.RiskItemSummary:
    return models.RiskItemSummary(
        **_risk_item_fields(item),
        factors=[
            models.RiskFactorSummary(rule=f.rule, points=f.points, value=f.value,
                                     confidence=f.confidence, message=f.message,
                                     caveats=list(f.caveats))
            for f in item.factors
        ],
    )  # fmt: skip


def risk_item_detail(item: RiskItem) -> models.RiskItemDetail:
    return models.RiskItemDetail(
        **_risk_item_fields(item),
        factors=[
            models.RiskFactorDetail(rule=f.rule, points=f.points, value=f.value,
                                    confidence=f.confidence, message=f.message,
                                    caveats=list(f.caveats), evidence=list(f.evidence))
            for f in item.factors
        ],
    )  # fmt: skip
