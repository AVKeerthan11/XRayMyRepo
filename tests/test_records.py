"""Record-level validation: evidence, nodes, edges, classifications, diagnostics."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from xraymyrepo.cim import (
    EDGE_RULES,
    Basis,
    Classification,
    ConfigEvidence,
    ConventionEvidence,
    Edge,
    EdgeKind,
    ExtractionIssue,
    ImportsAttributes,
    Node,
    UnresolvedReference,
)
from xraymyrepo.cim.evidence import Evidence

from .conftest import prov, span_ev

# CIM v1 decision: allowed bases for stored edges. Raw references that do not
# resolve to a concrete target are UnresolvedReferences, never observed edges.
LOCKED_EDGE_BASES = {
    EdgeKind.IMPORTS: {Basis.RESOLVED, Basis.HEURISTIC},
    EdgeKind.INHERITS: {Basis.RESOLVED, Basis.HEURISTIC},
    EdgeKind.CALLS: {Basis.RESOLVED, Basis.HEURISTIC},
    EdgeKind.REQUIRES: {Basis.OBSERVED},
    EdgeKind.HANDLES: {Basis.HEURISTIC},
    EdgeKind.TESTS: {Basis.HEURISTIC},
}

EVIDENCE: TypeAdapter[Any] = TypeAdapter(Evidence)


def node(**overrides: Any) -> Node:
    data: dict[str, Any] = {
        "key": "src/a.py#f",
        "kind": "function",
        "name": "f",
        "parent_key": "src/a.py",
        "language": "python",
        "span": {"start_line": 1, "start_col": 0, "end_line": 2, "end_col": 8},
        "basis": "observed",
        "confidence": "high",
        "provenance": prov(),
        "attributes": {"function_kind": "function"},
    }
    data.update(overrides)
    return Node.model_validate(data)


def edge(**overrides: Any) -> Edge:
    data: dict[str, Any] = {
        "kind": "IMPORTS",
        "source_key": "src/a.py",
        "target_key": "src/b.py",
        "basis": "resolved",
        "confidence": "high",
        "evidence": [span_ev()],
        "attributes": {"specifiers": ["./b"]},
    }
    data.update(overrides)
    return Edge.model_validate(data)


class TestEvidence:
    def test_span_evidence(self) -> None:
        item = EVIDENCE.validate_python(span_ev())
        assert item.type == "span"

    def test_span_must_be_ordered(self) -> None:
        bad = span_ev() | {"start_line": 5, "end_line": 4}
        with pytest.raises(ValidationError, match="precede"):
            EVIDENCE.validate_python(bad)

    def test_config_evidence_uses_json_pointer(self) -> None:
        item = EVIDENCE.validate_python(
            {"type": "config", "basis": "observed", "provenance": prov("manifest-extractor"),
             "file_key": "package.json", "json_pointer": "/dependencies/@scope~1pkg"}
        )  # fmt: skip
        assert isinstance(item, ConfigEvidence)
        with pytest.raises(ValidationError, match="JSON pointer"):
            EVIDENCE.validate_python(
                {"type": "config", "basis": "observed", "provenance": prov(),
                 "file_key": "package.json", "json_pointer": "dependencies"}
            )  # fmt: skip

    def test_convention_evidence_requires_rule(self) -> None:
        good = {"type": "convention", "basis": "heuristic", "provenance": prov(),
                "pattern": "test_*.py", "matched_value": "test_a.py"}  # fmt: skip
        assert isinstance(EVIDENCE.validate_python(good), ConventionEvidence)
        with pytest.raises(ValidationError, match="rule"):
            EVIDENCE.validate_python(good | {"provenance": prov(rule=None)})

    def test_heuristic_evidence_requires_rule(self) -> None:
        with pytest.raises(ValidationError, match="rule"):
            EVIDENCE.validate_python(span_ev(basis="heuristic", rule=None))

    def test_derived_evidence_must_reference_something(self) -> None:
        base = {"type": "derived", "basis": "inferred", "provenance": prov()}
        EVIDENCE.validate_python(base | {"node_keys": ["src/a.py"]})
        with pytest.raises(ValidationError, match="at least one"):
            EVIDENCE.validate_python(base)

    def test_ai_evidence_is_always_interpreted(self) -> None:
        ai = {"type": "ai", "basis": "interpreted", "model": "claude-opus-5-5",
              "prompt_version": "explain-v1",
              "cited_evidence_refs": [{"ref": "node", "key": "src/a.py"}]}  # fmt: skip
        assert EVIDENCE.validate_python(ai).basis is Basis.INTERPRETED
        with pytest.raises(ValidationError):
            EVIDENCE.validate_python(ai | {"basis": "observed"})
        with pytest.raises(ValidationError, match="only 'ai'"):
            EVIDENCE.validate_python(span_ev(basis="interpreted"))

    def test_no_embedded_source(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            EVIDENCE.validate_python(span_ev() | {"snippet": "import b"})

    def test_rule_id_grammar(self) -> None:
        for bad in ("py", "Py.Import", "py..import", "py.import "):
            with pytest.raises(ValidationError):
                EVIDENCE.validate_python(span_ev(rule=bad))


class TestNode:
    def test_valid_function(self) -> None:
        assert node().attributes.function_kind == "function"  # type: ignore[attr-defined]

    def test_groups_are_not_snapshot_nodes(self) -> None:
        with pytest.raises(ValidationError, match="derivation tier"):
            node(key="group:directory:src", kind="group", parent_key=None, span=None)

    def test_unknown_kind(self) -> None:
        with pytest.raises(ValidationError):
            node(kind="module")

    @pytest.mark.parametrize("basis", ["inferred", "interpreted"])
    def test_snapshot_nodes_reject_derived_bases(self, basis: str) -> None:
        with pytest.raises(ValidationError, match="cannot have basis"):
            node(basis=basis)

    def test_attributes_are_typed_by_kind(self) -> None:
        with pytest.raises(ValidationError):
            node(attributes={"function_kind": "lambda"})
        with pytest.raises(ValidationError):
            node(attributes={"type_kind": "class"})

    def test_key_must_match_kind_and_name(self) -> None:
        with pytest.raises(ValidationError):
            node(key="src/a.py")  # a path is not a function key
        with pytest.raises(ValidationError, match="name must be 'f'"):
            node(name="g")

    def test_files_carry_coverage_and_blob(self) -> None:
        file = node(key="src/a.py", kind="file", name="a.py", parent_key="src", span=None,
                    extraction_status="full", blob_sha="e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
                    attributes={"module_name": "a"})  # fmt: skip
        assert file.extraction_status == "full"
        with pytest.raises(ValidationError, match="extraction_status"):
            node(key="src/a.py", kind="file", name="a.py", parent_key="src", span=None,
                 blob_sha="e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", attributes={})  # fmt: skip
        with pytest.raises(ValidationError, match="extraction_status"):
            node(extraction_status="full")

    def test_symbols_need_spans(self) -> None:
        with pytest.raises(ValidationError, match="span"):
            node(span=None)

    def test_fingerprints_only_on_symbols(self) -> None:
        digest = "sha256:" + "a" * 64
        assert node(content_hash=digest, signature_hash=digest).content_hash == digest
        with pytest.raises(ValidationError, match="fingerprints"):
            node(key="src", kind="directory", name="src", parent_key="/", span=None,
                 attributes={}, content_hash=digest)  # fmt: skip

    def test_observed_means_high_confidence(self) -> None:
        with pytest.raises(ValidationError, match="high confidence"):
            node(confidence="medium")

    def test_non_observed_nodes_need_evidence(self) -> None:
        endpoint = {
            "key": "endpoint:http:GET /users/{id}@/", "kind": "endpoint", "name": "GET /users/{id}",
            "parent_key": "src/routes.py", "span": {"start_line": 3, "start_col": 0,
            "end_line": 3, "end_col": 20}, "basis": "heuristic", "confidence": "high",
            "provenance": prov(rule="py.fastapi.route_decorator"),
            "attributes": {"protocol": "http", "method": "GET", "route": "/users/{id}",
                           "scope_key": "/"},
        }  # fmt: skip
        with pytest.raises(ValidationError, match="need evidence"):
            Node.model_validate(endpoint)
        ok = Node.model_validate(
            endpoint | {"evidence": [span_ev("heuristic", "src/routes.py", 3)]}
        )
        assert ok.basis is Basis.HEURISTIC
        with pytest.raises(ValidationError, match="strongest"):
            Node.model_validate(endpoint | {"evidence": [span_ev("observed", "src/routes.py")]})

    def test_endpoint_key_must_match_attributes(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            Node.model_validate({
                "key": "endpoint:http:GET /users/{id}@/", "kind": "endpoint",
                "name": "GET /users/{id}", "parent_key": "r.py",
                "span": {"start_line": 1, "start_col": 0, "end_line": 1, "end_col": 1},
                "basis": "heuristic", "confidence": "high", "provenance": prov(),
                "evidence": [span_ev("heuristic", "r.py")],
                "attributes": {"protocol": "http", "method": "POST", "route": "/users/{id}",
                               "scope_key": "/"},
            })  # fmt: skip

    def test_external_packages_live_outside_the_tree(self) -> None:
        ext = {"key": "ext:npm:react", "kind": "external_package", "name": "react",
               "basis": "observed", "confidence": "high", "provenance": prov(),
               "attributes": {"ecosystem": "npm", "package_name": "react"}}  # fmt: skip
        assert Node.model_validate(ext | {"parent_key": None}).parent_key is None
        with pytest.raises(ValidationError, match="must not have a parent"):
            Node.model_validate(ext | {"parent_key": "/"})

    def test_frozen(self) -> None:
        with pytest.raises(ValidationError):
            node().name = "g"


class TestEdge:
    def test_valid_import(self) -> None:
        e = edge()
        assert isinstance(e.attributes, ImportsAttributes)
        assert e.ref.kind is EdgeKind.IMPORTS

    @pytest.mark.parametrize("kind", ["DECLARES", "DEPENDS_ON", "REFERENCES", "CONTAINS"])
    def test_unknown_kinds(self, kind: str) -> None:
        with pytest.raises(ValidationError):
            edge(kind=kind)

    def test_basis_is_strongest_evidence(self) -> None:
        mixed = [span_ev("heuristic"), span_ev("resolved", line=2)]
        assert edge(evidence=mixed, occurrence_count=2).basis is Basis.RESOLVED
        with pytest.raises(ValidationError, match="strongest"):
            edge(basis="heuristic", evidence=mixed, occurrence_count=2)

    def test_basis_allowed_per_kind(self) -> None:
        with pytest.raises(ValidationError, match="IMPORTS edges cannot have basis 'observed'"):
            edge(basis="observed", evidence=[span_ev("observed")])

    def test_allowed_bases_are_locked(self) -> None:
        assert {kind: rule.bases for kind, rule in EDGE_RULES.items()} == LOCKED_EDGE_BASES

    @pytest.mark.parametrize(
        ("kind", "basis"),
        [(k, b) for k in EdgeKind for b in ("observed", "resolved", "heuristic")],
    )
    def test_every_kind_rejects_bases_outside_the_lock(self, kind: EdgeKind, basis: str) -> None:
        endpoints = {EdgeKind.REQUIRES: ("pkg:npm:a", "ext:npm:b"),
                     EdgeKind.HANDLES: ("src/a.py#f", "endpoint:http:GET /x@/"),
                     EdgeKind.INHERITS: ("src/a.py#A", "src/b.py#B")}  # fmt: skip
        attributes = {EdgeKind.IMPORTS: {"specifiers": ["./b"]},
                      EdgeKind.REQUIRES: {"scope": "prod"}, EdgeKind.INHERITS: {"mode": "extends"},
                      EdgeKind.HANDLES: {}, EdgeKind.TESTS: {},
                      EdgeKind.CALLS: {"call_kind": "direct"}}  # fmt: skip
        source, target = endpoints.get(kind, ("src/a.py", "src/b.py"))
        data = {"kind": kind, "source_key": source, "target_key": target, "basis": basis,
                "evidence": [span_ev(basis)], "attributes": attributes[kind]}  # fmt: skip
        if Basis(basis) in LOCKED_EDGE_BASES[kind]:
            assert edge(**data).basis == basis
        else:
            with pytest.raises(ValidationError, match="cannot have basis"):
                edge(**data)

    def test_ai_cannot_become_an_edge(self) -> None:
        ai = {"type": "ai", "basis": "interpreted", "model": "m", "prompt_version": "v1",
              "cited_evidence_refs": [{"ref": "node", "key": "src/a.py"}]}  # fmt: skip
        with pytest.raises(ValidationError):
            edge(evidence=[ai])
        with pytest.raises(ValidationError):
            edge(basis="interpreted")

    def test_evidence_is_required_and_capped(self) -> None:
        with pytest.raises(ValidationError):
            edge(evidence=[])
        many = [span_ev(line=i) for i in range(1, 22)]
        with pytest.raises(ValidationError):
            edge(evidence=many, occurrence_count=21)

    def test_occurrences_cover_the_span_sample(self) -> None:
        two = [span_ev(line=1), span_ev(line=2)]
        assert edge(evidence=two, occurrence_count=57).occurrence_count == 57
        with pytest.raises(ValidationError, match="occurrence_count"):
            edge(evidence=two, occurrence_count=1)

    def test_self_loops_only_for_calls(self) -> None:
        with pytest.raises(ValidationError, match="self-loop"):
            edge(target_key="src/a.py")
        calls = edge(kind="CALLS", source_key="src/a.py#f", target_key="src/a.py#f",
                     attributes={"call_kind": "direct"})  # fmt: skip
        assert calls.source_key == calls.target_key

    def test_ambiguous_candidates_are_low_confidence(self) -> None:
        with pytest.raises(ValidationError, match="low confidence"):
            edge(ambiguity_group="ref-1")
        assert edge(ambiguity_group="ref-1", confidence="low").ambiguity_group == "ref-1"

    def test_import_attribute_lists_are_canonical(self) -> None:
        with pytest.raises(ValidationError, match="sorted"):
            edge(attributes={"specifiers": ["b", "a"]})
        with pytest.raises(ValidationError):
            edge(attributes={"specifiers": []})

    def test_attributes_typed_by_kind(self) -> None:
        with pytest.raises(ValidationError):
            edge(kind="REQUIRES", attributes={"specifiers": ["x"]})


class TestClassification:
    def classification(self, **overrides: Any) -> Classification:
        data: dict[str, Any] = {
            "node_key": "tests/test_a.py", "facet": "role", "value": "test_file",
            "basis": "heuristic", "confidence": "high",
            "evidence": [{"type": "convention", "basis": "heuristic", "provenance": prov(),
                          "pattern": "test_*.py", "matched_value": "test_a.py"}],
        }  # fmt: skip
        data.update(overrides)
        return Classification.model_validate(data)

    def test_valid(self) -> None:
        assert self.classification().value == "test_file"

    def test_values_are_checked_per_facet(self) -> None:
        with pytest.raises(ValidationError):
            self.classification(value="data_model")
        with pytest.raises(ValidationError):
            self.classification(facet="origin", value="test_file")
        assert self.classification(facet="layer", value="domain").value == "domain"
        with pytest.raises(ValidationError, match="slug"):
            self.classification(facet="layer", value="Domain Layer")

    def test_snapshot_layers_are_heuristic_only(self) -> None:
        layer = {"facet": "layer", "value": "domain"}
        assert self.classification(**layer).basis is Basis.HEURISTIC
        for basis in ("observed", "resolved", "inferred"):
            with pytest.raises(ValidationError, match="derivation run"):
                self.classification(**layer, basis=basis, evidence=[
                    {"type": "convention", "basis": basis, "provenance": prov(),
                     "pattern": "**/domain/**", "matched_value": "src/domain/a.py"}])  # fmt: skip

    def test_snapshot_layers_cannot_rest_on_graph_inference(self) -> None:
        derived = {"type": "derived", "basis": "heuristic", "provenance": prov(),
                   "edge_refs": [{"ref": "edge", "kind": "IMPORTS", "source_key": "src/a.py",
                                  "target_key": "src/b.py"}]}  # fmt: skip
        with pytest.raises(ValidationError, match="derived"):
            self.classification(facet="layer", value="domain", evidence=[derived])
        # Derived evidence remains fine for roles (e.g. orm_entity from INHERITS edges).
        role = self.classification(node_key="src/a.py#User", value="orm_entity",
                                   evidence=[derived])  # fmt: skip
        assert role.value == "orm_entity"

    def test_classifications_cannot_be_ai_or_inferred_in_snapshots(self) -> None:
        for basis in ("inferred", "interpreted"):
            with pytest.raises(ValidationError):
                self.classification(basis=basis)


class TestDiagnostics:
    def unresolved(self, **overrides: Any) -> UnresolvedReference:
        data: dict[str, Any] = {
            "source_key": "src/a.py", "file_key": "src/a.py", "ref_kind": "import",
            "raw_text": "missing_mod", "start_line": 1, "start_col": 0, "end_line": 1,
            "end_col": 18, "reason": "not_found", "provenance": prov(),
        }  # fmt: skip
        data.update(overrides)
        return UnresolvedReference.model_validate(data)

    def test_unresolved_reference(self) -> None:
        assert self.unresolved().reason == "not_found"
        for reason in ("dynamic", "unsupported_language"):
            assert self.unresolved(reason=reason).reason == reason

    def test_ambiguous_needs_candidates(self) -> None:
        with pytest.raises(ValidationError, match="two candidates"):
            self.unresolved(reason="ambiguous", candidates=["src/b.py"])
        ok = self.unresolved(reason="ambiguous", candidates=["lib/b.py", "src/b.py"])
        assert len(ok.candidates) == 2
        with pytest.raises(ValidationError, match="only ambiguous"):
            self.unresolved(candidates=["lib/b.py", "src/b.py"])

    def test_unknown_reason(self) -> None:
        with pytest.raises(ValidationError):
            self.unresolved(reason="timeout")

    def test_extraction_issue(self) -> None:
        issue = ExtractionIssue.model_validate(
            {"file_key": "src/a.py", "severity": "error", "code": "parse_error",
             "message": "invalid syntax", "provenance": prov(),
             "span": {"start_line": 1, "start_col": 4, "end_line": 1, "end_col": 5}}
        )  # fmt: skip
        assert issue.code == "parse_error"
        with pytest.raises(ValidationError, match="span needs a file_key"):
            ExtractionIssue.model_validate(
                {"severity": "error", "code": "x", "message": "m", "provenance": prov(),
                 "span": {"start_line": 1, "start_col": 0, "end_line": 1, "end_col": 1}}
            )  # fmt: skip
