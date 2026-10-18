"""The closed vocabularies are exactly the agreed CIM v1 sets: no more, no less."""

from __future__ import annotations

from xraymyrepo.cim import (
    Basis,
    ClassificationFacet,
    Confidence,
    EdgeChangeKind,
    EdgeKind,
    EndpointProtocol,
    FunctionKind,
    Incomparability,
    MatchMethod,
    NodeChangeKind,
    NodeKind,
    Origin,
    RiskCaveat,
    RiskFactorRule,
    RiskLevel,
    Role,
    Side,
    SnapshotStatus,
    TypeKind,
    UnresolvedReason,
)
from xraymyrepo.cim.enums import DERIVATION_BASES, SNAPSHOT_BASES, SNAPSHOT_NODE_KINDS


def values(enum: type) -> set[str]:
    return {member.value for member in enum}  # type: ignore[attr-defined]


def test_node_kinds_are_closed() -> None:
    assert values(NodeKind) == {
        "repository", "directory", "file", "type", "function",
        "endpoint", "package", "external_package", "group",
    }  # fmt: skip


def test_concepts_that_are_not_node_kinds() -> None:
    for not_a_kind in (
        "module",
        "class",
        "method",
        "route",
        "data_model",
        "config_file",
        "test_suite",
        "test_case",
        "component",
        "service",
        "layer",
        "commit",
    ):
        assert not_a_kind not in values(NodeKind)


def test_groups_are_not_snapshot_nodes() -> None:
    assert NodeKind.GROUP not in SNAPSHOT_NODE_KINDS


def test_type_function_and_protocol_kinds() -> None:
    assert values(TypeKind) == {
        "class", "interface", "struct", "enum", "trait", "protocol", "type_alias",
    }  # fmt: skip
    assert values(FunctionKind) == {"function", "method", "constructor", "accessor"}
    assert values(EndpointProtocol) == {"http", "grpc", "graphql", "websocket", "cli", "message"}


def test_edge_kinds_are_closed() -> None:
    assert values(EdgeKind) == {"IMPORTS", "REQUIRES", "INHERITS", "HANDLES", "TESTS", "CALLS"}
    for deferred in (
        "DECLARES",
        "DEPENDS_ON",
        "REFERENCES",
        "OVERRIDES",
        "READS_FROM",
        "WRITES_TO",
        "USES_TYPE",
        "CONTAINS",
        "MEMBER_OF",
    ):
        assert deferred not in values(EdgeKind)


def test_basis_and_confidence() -> None:
    assert [b.value for b in sorted(Basis, key=lambda b: -b.strength)] == [
        "observed", "resolved", "heuristic", "inferred", "interpreted",
    ]  # fmt: skip
    assert values(Confidence) == {"high", "medium", "low"}


def test_tiers_never_admit_ai_basis() -> None:
    assert Basis.INTERPRETED not in SNAPSHOT_BASES
    assert Basis.INTERPRETED not in DERIVATION_BASES
    assert Basis.INFERRED not in SNAPSHOT_BASES
    assert Basis.INFERRED in DERIVATION_BASES


def test_classification_vocabularies() -> None:
    assert values(ClassificationFacet) == {"role", "origin", "layer"}
    assert values(Role) == {
        "test_file", "test_function", "orm_entity", "schema", "config", "entrypoint", "manifest",
    }  # fmt: skip
    assert values(Origin) == {"source", "generated", "vendored", "docs", "build_artifact"}


def test_lifecycle_and_matching_vocabularies() -> None:
    assert values(SnapshotStatus) == {"pending", "complete", "failed"}
    assert values(UnresolvedReason) == {"not_found", "dynamic", "ambiguous", "unsupported_language"}
    assert values(MatchMethod) == {"same_key", "git_rename", "same_content_hash"}


def test_comparison_vocabularies_are_closed() -> None:
    assert values(NodeChangeKind) == {
        "added", "removed", "modified", "moved", "unchanged", "undetermined",
    }  # fmt: skip
    assert values(EdgeChangeKind) == {"added", "removed", "modified", "unchanged", "undetermined"}
    assert values(Incomparability) == {"extractor_version_differs", "config_differs"}


def test_change_risk_vocabularies_are_closed() -> None:
    assert values(RiskLevel) == {"low", "medium", "high"}  # no "safe", no "unknown"
    assert values(RiskCaveat) == {
        "incomparable_snapshots", "undetermined_changes", "incomplete_extraction",
        "hidden_dependencies", "impact_bounded", "heuristic_paths",
        "resolution_not_comparable", "dependent_resolution_changed",
    }  # fmt: skip
    assert values(RiskFactorRule) == {
        "risk.change.kind", "risk.change.interface", "risk.impact.dependents",
        "risk.impact.endpoints", "risk.change.sensitive_role", "risk.health.hub",
        "risk.health.cycle", "risk.dependency.unresolved_after_removal",
        "risk.test.no_dependent_test", "risk.cap.test_file",
    }  # fmt: skip
    assert values(Side) == {"from", "to"}
