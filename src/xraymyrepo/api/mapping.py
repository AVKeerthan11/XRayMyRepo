"""CIM and persistence records -> API models. The only place the two vocabularies meet.

The mappers are pure; ``summaries`` additionally fetches what a page of node
summaries needs (child counts and tags) in two batched queries, whatever the page
size.
"""

from __future__ import annotations

from collections.abc import Sequence

from xraymyrepo.cim import Classification, ClassificationFacet, Edge, EdgeKind
from xraymyrepo.cim.nodes import Node
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
