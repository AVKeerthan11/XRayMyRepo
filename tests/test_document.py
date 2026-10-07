"""Cross-record invariants of SnapshotDocument, exercised by mutating the golden document."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from xraymyrepo.cim import Node, SnapshotDocument, SpanEvidence, endpoint_scope

Data = dict[str, Any]


def find(items: list[Data], **match: Any) -> Data:
    return next(i for i in items if all(i.get(k) == v for k, v in match.items()))


def edge(data: Data, kind: str, source: str, target: str) -> Data:
    return find(data["edges"], kind=kind, source_key=source, target_key=target)


def _drop_node(key: str) -> Callable[[Data], None]:
    def mutate(d: Data) -> None:
        d["nodes"] = [n for n in d["nodes"] if n["key"] != key]

    return mutate


def _set(collection: str, match: Data, **changes: Any) -> Callable[[Data], None]:
    def mutate(d: Data) -> None:
        find(d[collection], **match).update(changes)

    return mutate


def _append_copy(collection: str, match: Data) -> Callable[[Data], None]:
    def mutate(d: Data) -> None:
        d[collection].append(dict(find(d[collection], **match)))

    return mutate


def _config_language(d: Data) -> None:
    d["config"]["languages"] = ["python"]


def _undeclared_producer(d: Data) -> None:
    d["producers"] = [p for p in d["producers"] if p["name"] != "manifest-extractor"]


def _no_coverage(d: Data) -> None:
    d["coverage"] = [c for c in d["coverage"] if c["language"] != "typescript"]


def _second_origin(d: Data) -> None:
    clone = dict(find(d["classifications"], node_key="scripts/deploy.sh", facet="origin"))
    clone["value"] = "generated"
    d["classifications"].append(clone)


def _contested_origins(d: Data) -> None:
    original = find(d["classifications"], node_key="scripts/deploy.sh", facet="origin")
    original["contested"] = True
    d["classifications"].append(original | {"value": "build_artifact"})


def _missing_origin(d: Data) -> None:
    d["classifications"] = [
        c for c in d["classifications"]
        if not (c["node_key"] == "scripts/deploy.sh" and c["facet"] == "origin")
    ]  # fmt: skip


def _edge_from_vendored(d: Data) -> None:
    template = edge(d, "IMPORTS", "frontend/src/index.ts", "ext:npm:express")
    d["edges"].append(template | {"source_key": "frontend/vendor/leftpad.js"})


def _symbol_in_failed_file(d: Data) -> None:
    template = find(d["nodes"], key="backend/app/plugins.py#load_plugin")
    d["nodes"].append(
        template | {"key": "backend/app/legacy.py#broken", "name": "broken",
                    "parent_key": "backend/app/legacy.py"}
    )  # fmt: skip


def _drop_parse_issue(d: Data) -> None:
    d["extraction_issues"] = []


def _wrong_symbol_parent(d: Data) -> None:
    find(d["nodes"], key="backend/app/users/service.py#UserService.get")["parent_key"] = (
        "backend/app/users/service.py"
    )


def _file_under_file(d: Data) -> None:
    find(d["nodes"], key="backend/app/db.py")["parent_key"] = "backend/app/main.py"


def _symbol_language(d: Data) -> None:
    find(d["nodes"], key="backend/app/db.py#Base")["language"] = "typescript"


def _handles_reversed(d: Data) -> None:
    e = edge(d, "HANDLES", "backend/app/users/routes.py#get_user",
             "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend")  # fmt: skip
    e["source_key"], e["target_key"] = e["target_key"], e["source_key"]


def _requires_from_file(d: Data) -> None:
    edge(d, "REQUIRES", "pkg:npm:@acme/web", "ext:npm:express")["source_key"] = (
        "frontend/package.json"
    )


def _external_inherits_without_symbol(d: Data) -> None:
    edge(d, "INHERITS", "backend/app/db.py#Base", "ext:pypi:sqlalchemy")["attributes"] = {
        "mode": "extends"
    }


def _single_ambiguity_candidate(d: Data) -> None:
    e = edge(d, "IMPORTS", "backend/app/main.py", "backend/app/users/routes.py")
    e.update(ambiguity_group="main-router", confidence="low")


def _evidence_unknown_file(d: Data) -> None:
    e = edge(d, "IMPORTS", "backend/app/main.py", "ext:pypi:fastapi")
    e["evidence"][0]["file_key"] = "backend/app/nope.py"


def _derived_cites_missing_edge(d: Data) -> None:
    c = find(d["classifications"], node_key="backend/app/users/schemas.py#UserOut")
    c["evidence"][0]["edge_refs"][0]["target_key"] = "ext:pypi:fastapi"


def _role_on_wrong_kind(d: Data) -> None:
    find(d["classifications"], node_key="backend/app/users/models.py#User")["node_key"] = (
        "backend/app/users/models.py"
    )


def _manifest_role_missing(d: Data) -> None:
    d["classifications"] = [
        c for c in d["classifications"]
        if not (c["node_key"] == "frontend/package.json" and c["value"] == "manifest")
    ]  # fmt: skip


def _unresolved_outside_file(d: Data) -> None:
    find(d["unresolved_references"], reason="dynamic")["file_key"] = "backend/app/main.py"


def _vendored_but_extracted(d: Data) -> None:
    find(d["nodes"], key="frontend/vendor/leftpad.js")["extraction_status"] = "full"


USERS_ENDPOINT = "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend"


def _rescope_users_endpoint(d: Data, scope: str) -> None:
    key = f"endpoint:http:GET /users/{{user_id}}@{scope}"
    endpoint = find(d["nodes"], key=USERS_ENDPOINT)
    endpoint["key"] = key
    endpoint["attributes"]["scope_key"] = scope
    find(d["edges"], kind="HANDLES", target_key=USERS_ENDPOINT)["target_key"] = key


def _endpoint_in_wrong_package(d: Data) -> None:
    _rescope_users_endpoint(d, "pkg:npm:@acme/web")


def _endpoint_skips_its_package(d: Data) -> None:
    _rescope_users_endpoint(d, "/")


def _endpoint_key_without_scope_attribute(d: Data) -> None:
    del find(d["nodes"], key=USERS_ENDPOINT)["attributes"]["scope_key"]


def _graph_derived_layer(d: Data) -> None:
    orm = find(d["classifications"], node_key="backend/app/users/models.py#User")
    d["classifications"].append(orm | {"facet": "layer", "value": "domain"})


def _registration(d: Data, file_key: str, line: int) -> Data:
    endpoint = find(d["nodes"], key=USERS_ENDPOINT)
    site = dict(endpoint["evidence"][0]) | {"file_key": file_key, "start_line": line,
                                            "end_line": line}  # fmt: skip
    endpoint["evidence"].append(site)
    return endpoint


def _later_registration_is_parent(d: Data) -> None:
    # main.py sorts before users/routes.py, so it is the earliest registration.
    _registration(d, "backend/app/main.py", 3)


def _span_is_not_a_registration(d: Data) -> None:
    endpoint = find(d["nodes"], key=USERS_ENDPOINT)
    endpoint["span"] = endpoint["span"] | {"start_line": 8, "end_line": 8}


def _two_roots(d: Data) -> None:
    d["nodes"].append(find(d["nodes"], key="/") | {"name": "second"})


CASES: list[tuple[str, Callable[[Data], None], str]] = [
    ("config hash", _config_language, "config_hash"),
    ("schema version", lambda d: d.update(schema_version="2.0"), "schema_version"),
    ("undeclared producer", _undeclared_producer, "undeclared producer"),
    ("duplicate node", _append_copy("nodes", {"key": "backend/app/db.py"}), "duplicate node key"),
    ("duplicate edge", _append_copy("edges", {"kind": "TESTS"}), "duplicate edge"),
    ("two repository roots", _two_roots, "duplicate node key"),
    ("missing parent", _drop_node("backend/app/users"), "unknown node"),
    ("file inside a file", _file_under_file, "cannot be inside a file"),
    ("symbol parent mismatch", _wrong_symbol_parent, "must be declared in"),
    ("symbol language", _symbol_language, "must have the language"),
    ("dangling edge target", _drop_node("ext:pypi:pydantic"), "unknown node"),
    ("HANDLES direction", _handles_reversed, "source cannot be a endpoint"),
    ("REQUIRES from a file", _requires_from_file, "source cannot be a file"),
    ("external target without symbol", _external_inherits_without_symbol, "target_symbol"),
    ("lonely ambiguity group", _single_ambiguity_candidate, "at least two candidate"),
    ("evidence cites unknown file", _evidence_unknown_file, "unknown node"),
    ("derived evidence cites missing edge", _derived_cites_missing_edge, "missing edge"),
    ("role on wrong node kind", _role_on_wrong_kind, "not applicable to a file"),
    ("duplicate classification", _append_copy("classifications", {"node_key": "README.md"}),
     "duplicate classification"),
    ("uncontested conflicting origins", _second_origin, "not marked contested"),
    ("file without origin", _missing_origin, "no origin"),
    ("manifest without role", _manifest_role_missing, "not a manifest"),
    ("edge kind without coverage", _no_coverage, "does not declare"),
    ("edge from excluded file", _edge_from_vendored, "was not extracted"),
    ("symbols in failed file", _symbol_in_failed_file, "status is failed"),
    ("failed file without issue", _drop_parse_issue, "needs an error issue"),
    ("unresolved source outside file", _unresolved_outside_file, "is not in"),
    ("vendored file extracted", _vendored_but_extracted, "file_only"),
    ("endpoint scoped to another package", _endpoint_in_wrong_package, "nearest package"),
    ("endpoint scoped past its package", _endpoint_skips_its_package, "nearest package"),
    ("endpoint without scope attribute", _endpoint_key_without_scope_attribute, "scope_key"),
    ("layer from graph inference", _graph_derived_layer, "derivation run"),
    ("endpoint parent is not the earliest registration", _later_registration_is_parent,
     "earliest registration"),
    ("endpoint span is not a registration", _span_is_not_a_registration,
     "registration evidence"),
    ("unknown producer on edge", _set("edges", {"kind": "TESTS"}, evidence=[{
        "type": "convention", "basis": "heuristic", "provenance": {"producer": "ghost",
        "rule": "py.pytest.test_file"}, "pattern": "test_*.py",
        "matched_value": "test_users.py"}]), "undeclared producer"),
]  # fmt: skip


@pytest.mark.parametrize(("mutate", "message"), [(m, msg) for _, m, msg in CASES],
                         ids=[name for name, _, _ in CASES])  # fmt: skip
def test_invariant_violations_are_rejected(
    golden_data: Data, mutate: Callable[[Data], None], message: str
) -> None:
    mutate(golden_data)
    with pytest.raises(ValidationError, match=message):
        SnapshotDocument.model_validate(golden_data)


def test_contested_origins_are_allowed(golden_data: Data) -> None:
    _contested_origins(golden_data)
    doc = SnapshotDocument.model_validate(golden_data)
    assert len([c for c in doc.classifications_of("scripts/deploy.sh") if c.contested]) == 2


def test_ambiguous_candidate_edges_are_allowed(golden_data: Data) -> None:
    first = edge(golden_data, "IMPORTS", "backend/app/main.py", "backend/app/users/routes.py")
    first.update(ambiguity_group="main-router", confidence="low")
    golden_data["edges"].append(first | {"target_key": "backend/app/users/__init__.py"})
    doc = SnapshotDocument.model_validate(golden_data)
    assert len([e for e in doc.edges if e.ambiguity_group == "main-router"]) == 2


def test_endpoint_without_a_package_is_scoped_to_the_repository(golden_data: Data) -> None:
    golden_data["nodes"] = [n for n in golden_data["nodes"] if n["key"] != "pkg:pypi:acme-backend"]
    golden_data["edges"] = [
        e for e in golden_data["edges"] if e["source_key"] != "pkg:pypi:acme-backend"
    ]
    with pytest.raises(ValidationError, match="nearest package"):
        SnapshotDocument.model_validate(golden_data)
    _rescope_users_endpoint(golden_data, "/")
    doc = SnapshotDocument.model_validate(golden_data)
    assert doc.node("endpoint:http:GET /users/{user_id}@/").name == "GET /users/{user_id}"


def test_same_route_in_two_packages_is_two_endpoints(golden_data: Data) -> None:
    # Both packages register GET /health: the scope keeps the keys apart.
    express = find(golden_data["nodes"], key="endpoint:http:GET /orders/{id}@pkg:npm:@acme/web")
    fastapi = find(golden_data["nodes"], key=USERS_ENDPOINT)
    for template, scope in ((express, "pkg:npm:@acme/web"), (fastapi, "pkg:pypi:acme-backend")):
        attrs = template["attributes"] | {"route": "/health"}
        attrs.pop("raw_route", None)
        golden_data["nodes"].append(template | {
            "key": f"endpoint:http:GET /health@{scope}", "name": "GET /health",
            "attributes": attrs,
        })  # fmt: skip
    doc = SnapshotDocument.model_validate(golden_data)
    health = sorted(n.key for n in doc.nodes if n.name == "GET /health")
    assert health == [
        "endpoint:http:GET /health@pkg:npm:@acme/web",
        "endpoint:http:GET /health@pkg:pypi:acme-backend",
    ]


def test_duplicate_registrations_are_extra_span_evidence(golden_data: Data) -> None:
    # A later registration (backend/tests sorts after backend/app) is evidence, not a parent.
    _registration(golden_data, "backend/tests/test_users.py", 2)
    doc = SnapshotDocument.model_validate(golden_data)
    endpoint = doc.node(USERS_ENDPOINT)
    assert endpoint.parent_key == "backend/app/users/routes.py"
    assert {e.file_key for e in endpoint.evidence if isinstance(e, SpanEvidence)} == {
        "backend/app/users/routes.py", "backend/tests/test_users.py",
    }  # fmt: skip


def package(key: str, root: str) -> Node:
    ecosystem, name = key.split(":", 2)[1:]
    manifest = {"pypi": "pyproject.toml", "npm": "package.json"}.get(ecosystem, "manifest")
    return Node.model_validate({
        "key": key, "kind": "package", "name": name, "parent_key": None,
        "basis": "observed", "confidence": "high",
        "provenance": {"producer": "manifest-extractor", "rule": "test.manifest"},
        "attributes": {"ecosystem": ecosystem, "package_name": name,
                       "manifest_key": manifest if root == "/" else f"{root}/{manifest}",
                       "root_key": root},
    })  # fmt: skip


class TestEndpointScope:
    PY = package("pkg:pypi:svc", "svc")
    WEB = package("pkg:npm:@acme/svc-web", "svc")

    def test_nearest_enclosing_package(self) -> None:
        outer = package("pkg:pypi:outer", "apps")
        inner = package("pkg:pypi:inner", "apps/inner")
        packages = [outer, inner]
        assert endpoint_scope("apps/inner/api/routes.py", "python", packages) == "pkg:pypi:inner"
        assert endpoint_scope("apps/other/routes.py", "python", packages) == "pkg:pypi:outer"

    def test_repository_scope_without_a_package(self) -> None:
        assert endpoint_scope("src/routes.py", "python", [self.PY]) == "/"
        assert endpoint_scope("src/routes.py", "python", []) == "/"

    def test_shared_root_is_resolved_by_ecosystem(self) -> None:
        packages = [self.PY, self.WEB]
        assert endpoint_scope("svc/app/routes.py", "python", packages) == "pkg:pypi:svc"
        assert endpoint_scope("svc/web/index.ts", "typescript", packages) == (
            "pkg:npm:@acme/svc-web"
        )
        assert endpoint_scope("svc/web/index.js", "javascript", packages) == (
            "pkg:npm:@acme/svc-web"
        )

    def test_fallback_is_the_smallest_key(self) -> None:
        # No ecosystem serves the language: every package at the root is a candidate.
        assert endpoint_scope("svc/main.go", "go", [self.PY, self.WEB]) == "pkg:npm:@acme/svc-web"
        # Several packages of the matching ecosystem: still the smallest key.
        twin = package("pkg:pypi:svc-admin", "svc")
        assert endpoint_scope("svc/a.py", "python", [twin, self.PY, self.WEB]) == "pkg:pypi:svc"
        assert endpoint_scope("svc/a.py", None, [twin, self.WEB]) == "pkg:npm:@acme/svc-web"

    def test_package_rooted_at_the_repository(self) -> None:
        root = package("pkg:npm:app", "/")
        assert endpoint_scope("src/index.ts", "typescript", [root]) == "pkg:npm:app"


def test_heuristic_convention_layers_are_snapshot_facts(golden_data: Data) -> None:
    golden_data["classifications"].append({
        "node_key": "backend/app/users/service.py", "facet": "layer", "value": "domain",
        "basis": "heuristic", "confidence": "medium",
        "evidence": [{"type": "convention", "basis": "heuristic",
                      "provenance": {"producer": "python-extractor",
                                     "rule": "layer.path_convention"},
                      "pattern": "**/service.py", "matched_value": "backend/app/users/service.py"}],
    })  # fmt: skip
    doc = SnapshotDocument.model_validate(golden_data)
    (layer,) = [c for c in doc.classifications if c.facet == "layer"]
    assert (layer.value, layer.basis) == ("domain", "heuristic")


def test_documents_cannot_carry_annotations(golden_data: Data) -> None:
    golden_data["annotations"] = []
    with pytest.raises(ValidationError, match="Extra inputs"):
        SnapshotDocument.model_validate(golden_data)
