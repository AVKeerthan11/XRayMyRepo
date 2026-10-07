"""Snapshot identity, derivation and interpretation tiers, lineage and fingerprints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from xraymyrepo.cim import (
    AnalysisConfig,
    Annotation,
    Basis,
    Derivation,
    Finding,
    FunctionKind,
    GroupEdge,
    InferredLayer,
    NodeLineage,
    ParameterShape,
    RepositoryRef,
    SignatureShape,
    Snapshot,
    SnapshotIdentity,
    SnapshotStatus,
    TypeKind,
    TypeShape,
    content_hash,
    signature_hash,
)
from xraymyrepo.cim.fingerprint import ParameterKind
from xraymyrepo.cim.snapshot import DEFAULT_EXCLUSION_POLICY

from .conftest import SHA, prov, span_ev

REPO = RepositoryRef(host="github.com", owner="acme", name="app")


def config(**overrides: Any) -> AnalysisConfig:
    data: dict[str, Any] = {
        "languages": ("python", "typescript"),
        "exclusion_policy": DEFAULT_EXCLUSION_POLICY,
    }
    data.update(overrides)
    return AnalysisConfig.model_validate(data)


def identity(
    commit: str = SHA, version: str = "1.0.0", cfg: AnalysisConfig | None = None
) -> SnapshotIdentity:
    return SnapshotIdentity(
        repository=REPO,
        commit_sha=commit,
        extractor_version=version,
        config_hash=(cfg or config()).config_hash(),
    )


class TestSnapshot:
    def test_identity_is_the_four_tuple(self) -> None:
        assert identity().as_tuple() == (
            "github.com/acme/app", SHA, "1.0.0", config().config_hash(),
        )  # fmt: skip

    def test_every_component_distinguishes_snapshots(self) -> None:
        base = identity()
        assert base == identity()
        assert base != identity(commit="f" * 40)
        assert base != identity(version="1.1.0")
        assert base != identity(cfg=config(ignore_globs=("dist/**",)))

    def test_config_hash_is_canonical(self) -> None:
        reordered = dict(reversed(list(DEFAULT_EXCLUSION_POLICY.items())))
        assert config().config_hash() == config(exclusion_policy=reordered).config_hash()
        assert config().config_hash().startswith("sha256:")

    def test_config_must_record_full_exclusion_policy(self) -> None:
        with pytest.raises(ValidationError, match="every origin"):
            config(exclusion_policy={"source": "extract"})
        with pytest.raises(ValidationError):
            AnalysisConfig.model_validate({"languages": ["python"]})

    def test_identity_fields_are_validated(self) -> None:
        with pytest.raises(ValidationError):
            identity(commit="HEAD")
        with pytest.raises(ValidationError):
            identity(version="v1")
        with pytest.raises(ValidationError):
            SnapshotIdentity(repository=REPO, commit_sha=SHA, extractor_version="1.0.0",
                             config_hash="md5:abc")  # fmt: skip

    def test_lifecycle_is_one_way(self) -> None:
        pending = Snapshot(identity=identity())
        assert pending.status is SnapshotStatus.PENDING
        done = pending.complete()
        assert done.status is SnapshotStatus.COMPLETE
        assert pending.status is SnapshotStatus.PENDING  # transitions return new values
        failed = pending.fail("parser crashed")
        assert failed.failure_reason == "parser crashed"
        for terminal in (done, failed):
            with pytest.raises(ValueError, match="only pending"):
                terminal.complete()

    def test_snapshots_cannot_be_repointed(self) -> None:
        snapshot = Snapshot(identity=identity())
        with pytest.raises(ValidationError):
            snapshot.identity = identity(commit="f" * 40)
        with pytest.raises(ValidationError):
            snapshot.identity.commit_sha = "f" * 40

    def test_failed_needs_reason(self) -> None:
        with pytest.raises(ValidationError, match="failure_reason"):
            Snapshot(identity=identity(), status=SnapshotStatus.FAILED)


def derivation(**overrides: Any) -> Derivation:
    data: dict[str, Any] = {
        "run": {"snapshot": identity(), "algorithm": "directory-lens", "algorithm_version": "1.0.0",
                "params_hash": "sha256:" + "1" * 64},
        "lenses": [{"name": "directory", "kind": "directory"},
                   {"name": "inferred", "kind": "inferred"}],
        "groups": [
            {"key": "group:directory:src", "basis": "observed", "confidence": "high",
             "provenance": prov("directory-lens", "lens.directory"), "label": "src"},
            {"key": "group:directory:src/app", "parent_key": "group:directory:src",
             "basis": "observed", "confidence": "high",
             "provenance": prov("directory-lens", "lens.directory")},
            {"key": "group:inferred:cluster-1", "basis": "inferred", "confidence": "medium",
             "provenance": prov("louvain", "cluster.louvain"),
             "evidence": [{"type": "derived", "basis": "inferred",
                           "provenance": prov("louvain", "cluster.louvain"),
                           "node_keys": ["src/app/a.py", "src/app/b.py"]}]},
        ],
        "members": [
            {"group_key": "group:directory:src/app", "node_key": "src/app/a.py",
             "basis": "observed", "confidence": "high"},
            {"group_key": "group:inferred:cluster-1", "node_key": "src/app/a.py",
             "basis": "inferred", "confidence": "medium"},
        ],
    }  # fmt: skip
    data.update(overrides)
    return Derivation.model_validate(data)


class TestDerivation:
    def test_valid_lenses(self) -> None:
        d = derivation()
        assert {g.lens for g in d.groups} == {"directory", "inferred"}

    def test_inferred_lens_is_always_marked_inferred(self) -> None:
        bad_group = {"key": "group:inferred:cluster-2", "basis": "observed", "confidence": "high",
                     "provenance": prov("louvain", "cluster.louvain")}  # fmt: skip
        with pytest.raises(ValidationError, match="inferred"):
            derivation(groups=[bad_group])
        bad_member = {"group_key": "group:inferred:cluster-1", "node_key": "src/app/a.py",
                      "basis": "observed", "confidence": "high"}  # fmt: skip
        groups = derivation().model_dump(mode="json")["groups"]
        with pytest.raises(ValidationError, match="inferred"):
            derivation(groups=groups, members=[bad_member])

    def test_derivations_never_hold_ai_output(self) -> None:
        ai_group = {"key": "group:inferred:cluster-9", "basis": "interpreted",
                    "confidence": "low", "provenance": prov("llm")}  # fmt: skip
        with pytest.raises(ValidationError, match="cannot have basis"):
            derivation(groups=[ai_group])

    def test_lens_is_a_tree_partition(self) -> None:
        dup = [{"group_key": "group:directory:src", "node_key": "src/app/a.py",
                "basis": "observed", "confidence": "high"},
               {"group_key": "group:directory:src/app", "node_key": "src/app/a.py",
                "basis": "observed", "confidence": "high"}]  # fmt: skip
        with pytest.raises(ValidationError, match="two groups"):
            derivation(members=dup)

    def test_members_are_files(self) -> None:
        with pytest.raises(ValidationError):
            derivation(members=[{"group_key": "group:directory:src", "node_key": "src/a.py#f",
                                 "basis": "observed", "confidence": "high"}])  # fmt: skip

    def test_group_edges_keep_basis_counts(self) -> None:
        rollup = GroupEdge.model_validate({
            "source_group_key": "group:directory:src", "target_group_key": "group:directory:lib",
            "edge_kind": "IMPORTS", "count": 42, "basis_counts": {"resolved": 40, "heuristic": 2},
        })  # fmt: skip
        assert rollup.basis_counts[Basis.RESOLVED] == 40
        with pytest.raises(ValidationError, match="sum"):
            GroupEdge.model_validate(rollup.model_dump() | {"count": 41})
        with pytest.raises(ValidationError, match="same lens"):
            GroupEdge.model_validate(
                rollup.model_dump() | {"target_group_key": "group:inferred:cluster-1"}
            )
        with pytest.raises(ValidationError, match="inside one group"):
            GroupEdge.model_validate(
                rollup.model_dump() | {"target_group_key": "group:directory:src"}
            )

    def test_findings_need_evidence(self) -> None:
        finding = {"rule": "health.import_cycle", "severity": "medium",
                   "subject_keys": ["src/a.py", "src/b.py"], "message": "Import cycle",
                   "basis": "inferred", "confidence": "high",
                   "evidence": [{"type": "derived", "basis": "inferred",
                                 "provenance": prov("health", "health.import_cycle"),
                                 "node_keys": ["src/a.py"]}]}  # fmt: skip
        assert Finding.model_validate(finding).severity == "medium"
        with pytest.raises(ValidationError):
            Finding.model_validate(finding | {"evidence": []})


def inferred_layer(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "node_key": "src/app/users/service.py", "layer": "domain",
        "basis": "inferred", "confidence": "medium",
        "provenance": prov("layering", "layer.dependency_depth"),
        "evidence": [{"type": "derived", "basis": "inferred",
                      "provenance": prov("layering", "layer.dependency_depth"),
                      "edge_refs": [{"ref": "edge", "kind": "IMPORTS",
                                     "source_key": "src/app/users/routes.py",
                                     "target_key": "src/app/users/service.py"}]}],
    }  # fmt: skip
    data.update(overrides)
    return data


class TestInferredLayers:
    def test_inferred_layers_belong_to_a_derivation_run(self) -> None:
        d = derivation(layers=[inferred_layer()])
        (layer,) = d.layers
        assert (layer.layer, layer.basis) == ("domain", Basis.INFERRED)

    def test_inferred_layers_are_always_marked_inferred(self) -> None:
        for basis in ("observed", "resolved", "heuristic"):
            with pytest.raises(ValidationError, match="basis 'inferred'"):
                InferredLayer.model_validate(inferred_layer(basis=basis))

    def test_inferred_layers_name_their_rule_and_cite_derived_evidence(self) -> None:
        with pytest.raises(ValidationError, match="rule"):
            InferredLayer.model_validate(inferred_layer(provenance=prov("layering", None)))
        with pytest.raises(ValidationError):
            InferredLayer.model_validate(inferred_layer(evidence=[]))
        with pytest.raises(ValidationError):
            InferredLayer.model_validate(inferred_layer(evidence=[span_ev("heuristic")]))
        heuristic_derived = inferred_layer()["evidence"][0] | {"basis": "heuristic"}
        with pytest.raises(ValidationError, match="evidence must have basis 'inferred'"):
            InferredLayer.model_validate(inferred_layer(evidence=[heuristic_derived]))

    def test_inferred_layers_never_hold_ai_output(self) -> None:
        with pytest.raises(ValidationError):
            InferredLayer.model_validate(inferred_layer(basis="interpreted"))

    def test_layers_apply_to_paths_and_symbols(self) -> None:
        assert InferredLayer.model_validate(inferred_layer(node_key="src/a.py#f")).node_key
        assert InferredLayer.model_validate(inferred_layer(node_key="src/app")).node_key
        for bad in ("ext:npm:react", "endpoint:http:GET /x@/", "src/a.py#"):
            with pytest.raises(ValidationError):
                InferredLayer.model_validate(inferred_layer(node_key=bad))

    def test_one_inferred_layer_per_node_per_run(self) -> None:
        with pytest.raises(ValidationError, match="at most one"):
            derivation(layers=[inferred_layer(), inferred_layer(layer="api")])


def annotation(**overrides: Any) -> Annotation:
    data: dict[str, Any] = {
        "id": uuid.UUID(int=1),
        "snapshot": identity(),
        "subject": {"ref": "node", "key": "group:inferred:cluster-1"},
        "kind": "label",
        "content": "User management",
        "model": "claude-opus-5-5",
        "prompt_version": "label-v1",
        "citations": [{"ref": "node", "key": "src/app/users.py"},
                      {"ref": "span", "file_key": "src/app/users.py", "start_line": 1,
                       "end_line": 20}],
        "created_at": datetime(2026, 10, 6, tzinfo=UTC),
    }  # fmt: skip
    data.update(overrides)
    return Annotation.model_validate(data)


class TestInterpretation:
    def test_annotation_is_interpreted(self) -> None:
        assert annotation().basis is Basis.INTERPRETED

    def test_annotation_must_cite_facts(self) -> None:
        with pytest.raises(ValidationError):
            annotation(citations=[])

    def test_annotation_cannot_carry_graph_facts(self) -> None:
        for smuggled in ("nodes", "edges", "classifications", "basis", "evidence"):
            with pytest.raises(ValidationError, match="Extra inputs"):
                annotation(**{smuggled: []})

    def test_annotation_subject_is_a_node_or_edge(self) -> None:
        edge_subject = {"ref": "edge", "kind": "IMPORTS", "source_key": "a.py",
                        "target_key": "b.py"}  # fmt: skip
        assert annotation(subject=edge_subject).subject.ref == "edge"
        with pytest.raises(ValidationError):
            annotation(subject={"ref": "span", "file_key": "a.py", "start_line": 1,
                                "end_line": 1})  # fmt: skip

    def test_append_only_supersession(self) -> None:
        assert annotation(superseded_by=uuid.UUID(int=2)).superseded_by == uuid.UUID(int=2)
        with pytest.raises(ValidationError, match="itself"):
            annotation(superseded_by=uuid.UUID(int=1))
        with pytest.raises(ValidationError):
            annotation().content = "edited"

    def test_timestamps_are_aware(self) -> None:
        with pytest.raises(ValidationError, match="timezone"):
            annotation(created_at=datetime(2026, 10, 6))


class TestLineage:
    def lineage(self, **overrides: Any) -> NodeLineage:
        data: dict[str, Any] = {
            "from_snapshot": identity(), "from_key": "src/a.py#f",
            "to_snapshot": identity(commit="f" * 40), "to_key": "src/a.py#f",
            "match_method": "same_key", "confidence": "high",
        }  # fmt: skip
        data.update(overrides)
        return NodeLineage.model_validate(data)

    def test_methods(self) -> None:
        assert self.lineage().match_method == "same_key"
        moved = self.lineage(to_key="lib/a.py#f", match_method="git_rename")
        assert moved.to_key == "lib/a.py#f"
        renamed = self.lineage(to_key="src/a.py#g", match_method="same_content_hash",
                               confidence="medium")  # fmt: skip
        assert renamed.confidence == "medium"

    def test_method_consistency(self) -> None:
        with pytest.raises(ValidationError, match="identical keys"):
            self.lineage(to_key="src/a.py#g")
        with pytest.raises(ValidationError, match="different keys"):
            self.lineage(match_method="git_rename")
        with pytest.raises(ValidationError, match="high or medium"):
            self.lineage(to_key="src/a.py#g", match_method="same_content_hash", confidence="low")

    def test_no_fuzzy_matching_in_v1(self) -> None:
        with pytest.raises(ValidationError):
            self.lineage(match_method="similarity")

    def test_lineage_spans_two_snapshots_of_one_repository(self) -> None:
        with pytest.raises(ValidationError, match="different snapshots"):
            self.lineage(to_snapshot=identity())
        other = identity().model_copy(
            update={"repository": RepositoryRef(host="github.com", owner="acme", name="other")}
        )
        with pytest.raises(ValidationError, match="same repository"):
            self.lineage(to_snapshot=other)


class TestFingerprints:
    def test_content_hash_is_deterministic_and_token_based(self) -> None:
        tokens = ["def", "(", "x", ")", ":", "return", "x"]
        assert content_hash(tokens) == content_hash(list(tokens))
        assert content_hash(tokens) != content_hash(["def", "(", "x", ")", ":", "return", "y"])
        # Token boundaries matter: "ab" + "c" is not "a" + "bc".
        assert content_hash(["ab", "c"]) != content_hash(["a", "bc"])
        with pytest.raises(ValueError):
            content_hash(["ok", ""])

    def test_signature_hash_ignores_the_symbol_name(self) -> None:
        shape = SignatureShape(
            function_kind=FunctionKind.METHOD,
            parameters=(
                ParameterShape(name="self", kind=ParameterKind.POSITIONAL),
                ParameterShape(name="user_id", kind=ParameterKind.POSITIONAL, annotation="int"),
            ),
            returns="User | None",
        )
        # The shape type has no field for the symbol's own name.
        assert "name" not in SignatureShape.model_fields
        assert signature_hash(shape) == signature_hash(shape.model_copy())
        changed = shape.model_copy(update={"returns": "User"})
        assert signature_hash(shape) != signature_hash(changed)

    def test_type_and_callable_shapes_never_collide(self) -> None:
        assert signature_hash(TypeShape(type_kind=TypeKind.CLASS)) != signature_hash(
            SignatureShape(function_kind=FunctionKind.FUNCTION)
        )

    def test_fingerprints_are_sha256(self) -> None:
        assert content_hash(["x"]).startswith("sha256:")
        assert len(signature_hash(TypeShape(type_kind=TypeKind.ENUM))) == len("sha256:") + 64


def test_span_helper_is_valid_evidence() -> None:
    # Guards the shared test helper itself.
    assert span_ev()["type"] == "span"
