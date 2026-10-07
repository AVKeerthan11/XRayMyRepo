"""The golden fixture: a hand-written expected CIM for fixtures/golden/v1/repo.

These tests pin (1) that the golden document satisfies every contract invariant,
(2) that it is in sync with the fixture files byte for byte, and (3) the exact
facts a v1 extractor must produce for the important relationships.
"""

from __future__ import annotations

import hashlib
from collections import Counter

import pytest

from xraymyrepo.cim import (
    Basis,
    Confidence,
    ConfigEvidence,
    DerivedEvidence,
    EdgeKind,
    ExtractionStatus,
    ImportsAttributes,
    NodeKind,
    SnapshotDocument,
    SpanEvidence,
    UnresolvedReason,
)
from xraymyrepo.cim.keys import decode_path

from .conftest import GOLDEN_REPO

PY = "backend/app"
TS = "frontend/src"
USERS_ENDPOINT = "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend"
ORDERS_ENDPOINT = "endpoint:http:GET /orders/{id}@pkg:npm:@acme/web"


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def source(key: str) -> list[str]:
    return (GOLDEN_REPO / decode_path(key)).read_text(encoding="utf-8").split("\n")


def text_at(file_key: str, start_line: int, start_col: int, end_line: int, end_col: int) -> str:
    lines = source(file_key)
    if start_line == end_line:
        return lines[start_line - 1][start_col:end_col]
    middle = lines[start_line : end_line - 1]
    return "\n".join([lines[start_line - 1][start_col:], *middle, lines[end_line - 1][:end_col]])


# ---------------------------------------------------------------------------
# 1. Contract validity and canonical form


def test_golden_is_a_valid_snapshot_document(golden: SnapshotDocument) -> None:
    counts = Counter(n.kind for n in golden.nodes)
    assert counts == {
        NodeKind.REPOSITORY: 1, NodeKind.DIRECTORY: 10, NodeKind.FILE: 22,
        NodeKind.TYPE: 6, NodeKind.FUNCTION: 6, NodeKind.ENDPOINT: 2,
        NodeKind.PACKAGE: 2, NodeKind.EXTERNAL_PACKAGE: 7,
    }  # fmt: skip
    assert Counter(e.kind for e in golden.edges) == {
        EdgeKind.IMPORTS: 18, EdgeKind.REQUIRES: 6, EdgeKind.INHERITS: 3,
        EdgeKind.HANDLES: 2, EdgeKind.TESTS: 1,
    }  # fmt: skip


def test_canonical_json_round_trips(golden: SnapshotDocument) -> None:
    text = golden.to_canonical_json()
    again = SnapshotDocument.model_validate_json(text)
    assert again.canonical() == golden.canonical()
    assert again.to_canonical_json() == text


# ---------------------------------------------------------------------------
# 2. Golden <-> fixture synchronisation


def test_every_fixture_path_is_a_node_and_vice_versa(golden: SnapshotDocument) -> None:
    on_disk_files = {
        p.relative_to(GOLDEN_REPO).as_posix() for p in GOLDEN_REPO.rglob("*") if p.is_file()
    }
    on_disk_dirs = {
        p.relative_to(GOLDEN_REPO).as_posix() for p in GOLDEN_REPO.rglob("*") if p.is_dir()
    }
    files = {decode_path(n.key) for n in golden.nodes if n.kind is NodeKind.FILE}
    dirs = {decode_path(n.key) for n in golden.nodes if n.kind is NodeKind.DIRECTORY}
    assert files == on_disk_files
    assert dirs == on_disk_dirs


def test_blob_shas_match_fixture_bytes(golden: SnapshotDocument) -> None:
    for node in golden.nodes:
        if node.kind is NodeKind.FILE:
            data = (GOLDEN_REPO / decode_path(node.key)).read_bytes()
            assert b"\r\n" not in data, f"{node.key} has CRLF line endings"
            assert node.blob_sha == git_blob_sha(data), node.key


def _all_spans(golden: SnapshotDocument) -> list[tuple[str, int, int, int, int]]:
    spans = []
    for node in golden.nodes:
        if node.span is not None:
            assert node.parent_key is not None
            file_key = node.key.split("#")[0] if "#" in node.key else node.parent_key
            s = node.span
            spans.append((file_key, s.start_line, s.start_col, s.end_line, s.end_col))
    evidence = [e for n in golden.nodes for e in n.evidence]
    evidence += [e for edge in golden.edges for e in edge.evidence]
    evidence += [e for c in golden.classifications for e in c.evidence]
    for item in evidence:
        if isinstance(item, SpanEvidence):
            spans.append(
                (item.file_key, item.start_line, item.start_col, item.end_line, item.end_col)
            )
    for u in golden.unresolved_references:
        spans.append((u.file_key, u.start_line, u.start_col, u.end_line, u.end_col))
    return spans


def test_every_span_lies_inside_its_file(golden: SnapshotDocument) -> None:
    for file_key, sl, sc, el, ec in _all_spans(golden):
        lines = source(file_key)
        assert el <= len(lines), (file_key, el)
        assert sc <= len(lines[sl - 1]) and ec <= len(lines[el - 1]), (file_key, sl, el)
        snippet = text_at(file_key, sl, sc, el, ec)
        assert snippet.strip() == snippet and snippet, (file_key, sl, snippet)


@pytest.mark.parametrize(
    ("file_key", "span", "expected"),
    [
        (f"{PY}/users/models.py", (4, 0, 4, 21), "from ..db import Base"),
        (f"{PY}/users/models.py", (7, 11, 7, 15), "Base"),
        (f"{PY}/users/routes.py", (9, 0, 9, 49),
         '@router.get("/{user_id}", response_model=UserOut)'),
        (f"{PY}/users/routes.py", (6, 0, 6, 35), 'router = APIRouter(prefix="/users")'),
        (f"{PY}/plugins.py", (7, 11, 7, 40), "importlib.import_module(name)"),
        (f"{PY}/generated/user_pb2.py", (2, 0, 2, 58),
         "# Generated by the protocol buffer compiler.  DO NOT EDIT!"),
        (f"{TS}/index.ts", (7, 0, 7, 32), 'app.get("/orders/:id", getOrder)'),
        (f"{TS}/index.ts", (3, 0, 3, 36), 'import { getOrder } from "./orders";'),
        (f"{TS}/orders/index.ts", (2, 0, 2, 37), 'export type { Order } from "./types";'),
        ("backend/app/legacy.py", (1, 11, 1, 12), ":"),
    ],
)  # fmt: skip
def test_key_spans_point_at_the_intended_source(
    file_key: str, span: tuple[int, int, int, int], expected: str
) -> None:
    assert text_at(file_key, *span) == expected


# ---------------------------------------------------------------------------
# 3. Deterministic expectations for important facts


def test_containment_is_parent_keys_not_edges(golden: SnapshotDocument) -> None:
    method = golden.node(f"{PY}/users/service.py#UserService.get")
    assert method.parent_key == f"{PY}/users/service.py#UserService"
    assert golden.node(method.parent_key).parent_key == f"{PY}/users/service.py"
    assert golden.node(f"{PY}/users/service.py").parent_key == f"{PY}/users"
    assert golden.node("backend").parent_key == "/"
    assert golden.node("/").parent_key is None


def test_modules_are_file_attributes(golden: SnapshotDocument) -> None:
    attrs = golden.node(f"{PY}/users/service.py").attributes
    assert getattr(attrs, "module_name", None) == "app.users.service"


def test_symbol_kinds(golden: SnapshotDocument) -> None:
    def kind_of(key: str) -> str:
        attrs = golden.node(key).attributes
        return str(getattr(attrs, "type_kind", None) or getattr(attrs, "function_kind", None))

    assert kind_of(f"{PY}/users/service.py#UserService") == "class"
    assert kind_of(f"{PY}/users/service.py#UserService.__init__") == "constructor"
    assert kind_of(f"{PY}/users/service.py#UserService.get") == "method"
    assert kind_of(f"{TS}/orders/types.ts#Order") == "interface"
    assert kind_of(f"{TS}/orders/handlers.ts#getOrder") == "function"


def test_relative_import_resolves_to_the_symbol(golden: SnapshotDocument) -> None:
    edge = golden.edge(EdgeKind.IMPORTS, f"{PY}/users/models.py", f"{PY}/db.py#Base")
    assert (edge.basis, edge.confidence) == (Basis.RESOLVED, Confidence.HIGH)
    assert edge.attributes == ImportsAttributes(specifiers=("..db",), imported_names=("Base",))
    assert edge.evidence[0].provenance.rule == "py.import.relative"


def test_import_of_a_variable_targets_its_file(golden: SnapshotDocument) -> None:
    # `router` is a module-level variable; variables are not nodes in v1.
    edge = golden.edge(EdgeKind.IMPORTS, f"{PY}/main.py", f"{PY}/users/routes.py")
    assert edge.attributes == ImportsAttributes(
        specifiers=("app.users.routes",), imported_names=("router",)
    )


def test_repeated_imports_are_one_edge_with_occurrences(golden: SnapshotDocument) -> None:
    edge = golden.edge(EdgeKind.IMPORTS, f"{PY}/users/models.py", "ext:pypi:sqlalchemy")
    assert edge.occurrence_count == 2
    assert [e.start_line for e in edge.evidence if isinstance(e, SpanEvidence)] == [1, 2]


def test_barrel_import_resolves_through_to_the_definition(golden: SnapshotDocument) -> None:
    edge = golden.edge(EdgeKind.IMPORTS, f"{TS}/index.ts", f"{TS}/orders/handlers.ts#getOrder")
    assert isinstance(edge.attributes, ImportsAttributes)
    assert edge.attributes.via == (f"{TS}/orders/index.ts",)
    # No edge to the barrel file itself.
    assert not [e for e in golden.edges if e.target_key == f"{TS}/orders/index.ts"]
    reexport = golden.edge(EdgeKind.IMPORTS, f"{TS}/orders/index.ts", f"{TS}/orders/types.ts#Order")
    assert isinstance(reexport.attributes, ImportsAttributes)
    assert reexport.attributes.is_reexport and reexport.attributes.is_type_only


def test_type_only_imports(golden: SnapshotDocument) -> None:
    edge = golden.edge(EdgeKind.IMPORTS, f"{TS}/orders/handlers.ts", "ext:npm:express")
    assert isinstance(edge.attributes, ImportsAttributes)
    assert edge.attributes.is_type_only
    assert edge.attributes.imported_names == ("Request", "Response")


def test_inheritance_internal_and_external(golden: SnapshotDocument) -> None:
    internal = golden.edge(EdgeKind.INHERITS, f"{PY}/users/models.py#User", f"{PY}/db.py#Base")
    assert internal.basis is Basis.RESOLVED
    external = golden.edge(EdgeKind.INHERITS, f"{PY}/db.py#Base", "ext:pypi:sqlalchemy")
    assert getattr(external.attributes, "target_symbol", None) == "sqlalchemy.orm.DeclarativeBase"


def test_endpoints_are_generic_and_heuristic(golden: SnapshotDocument) -> None:
    fastapi = golden.node(USERS_ENDPOINT)
    express = golden.node(ORDERS_ENDPOINT)
    assert fastapi.basis is express.basis is Basis.HEURISTIC
    # Registration site, not the handler, is the endpoint's parent.
    assert fastapi.parent_key == f"{PY}/users/routes.py"
    assert express.parent_key == f"{TS}/index.ts"
    assert getattr(express.attributes, "raw_route", None) == "/orders/:id"
    # The router prefix is part of the evidence for the full path.
    rules = [e.provenance.rule for e in fastapi.evidence]
    assert rules == ["py.fastapi.route_decorator", "py.fastapi.router_prefix"]


def test_endpoints_are_scoped_to_their_nearest_package(golden: SnapshotDocument) -> None:
    fastapi = golden.node(USERS_ENDPOINT)
    express = golden.node(ORDERS_ENDPOINT)
    assert getattr(fastapi.attributes, "scope_key", None) == "pkg:pypi:acme-backend"
    assert getattr(express.attributes, "scope_key", None) == "pkg:npm:@acme/web"
    # The name is the semantic endpoint without its scope.
    assert (fastapi.name, express.name) == ("GET /users/{user_id}", "GET /orders/{id}")
    # Identity never mentions the handler function.
    for endpoint in (fastapi, express):
        (handles,) = [e for e in golden.edges_of(EdgeKind.HANDLES) if e.target_key == endpoint.key]
        handler_name = handles.source_key.rsplit("#", 1)[1]
        assert handler_name not in endpoint.key


def test_handles_edges(golden: SnapshotDocument) -> None:
    py = golden.edge(EdgeKind.HANDLES, f"{PY}/users/routes.py#get_user", USERS_ENDPOINT)
    assert (py.basis, py.confidence) == (Basis.HEURISTIC, Confidence.HIGH)
    ts = golden.edge(EdgeKind.HANDLES, f"{TS}/orders/handlers.ts#getOrder", ORDERS_ENDPOINT)
    derived = [e for e in ts.evidence if isinstance(e, DerivedEvidence)]
    assert [r.target_key for r in derived[0].edge_refs] == [f"{TS}/orders/handlers.ts#getOrder"]


def test_tests_edge_is_heuristic_with_medium_confidence(golden: SnapshotDocument) -> None:
    edge = golden.edge(
        EdgeKind.TESTS, "backend/tests/test_users.py", f"{PY}/users/service.py#UserService"
    )
    assert (edge.basis, edge.confidence) == (Basis.HEURISTIC, Confidence.MEDIUM)
    assert {e.type for e in edge.evidence} == {"span", "convention"}


def test_manifest_requirements(golden: SnapshotDocument) -> None:
    required = {
        (e.source_key, e.target_key, str(getattr(e.attributes, "scope", "")))
        for e in golden.edges_of(EdgeKind.REQUIRES)
    }
    assert required == {
        ("pkg:pypi:acme-backend", "ext:pypi:fastapi", "prod"),
        ("pkg:pypi:acme-backend", "ext:pypi:sqlalchemy", "prod"),  # declared as "SQLAlchemy"
        ("pkg:pypi:acme-backend", "ext:pypi:pydantic", "prod"),
        ("pkg:pypi:acme-backend", "ext:pypi:pytest", "dev"),
        ("pkg:npm:@acme/web", "ext:npm:express", "prod"),
        ("pkg:npm:@acme/web", "ext:npm:typescript", "dev"),
    }
    for edge in golden.edges_of(EdgeKind.REQUIRES):
        assert edge.basis is Basis.OBSERVED
        assert isinstance(edge.evidence[0], ConfigEvidence)


def test_roles_are_classifications(golden: SnapshotDocument) -> None:
    roles = {(c.node_key, c.value, c.basis) for c in golden.classifications if c.facet == "role"}
    assert roles == {
        ("backend/pyproject.toml", "manifest", Basis.OBSERVED),
        ("frontend/package.json", "manifest", Basis.OBSERVED),
        ("frontend/tsconfig.json", "config", Basis.HEURISTIC),
        (f"{PY}/main.py", "entrypoint", Basis.HEURISTIC),
        (f"{TS}/index.ts", "entrypoint", Basis.OBSERVED),
        (f"{PY}/users/models.py#User", "orm_entity", Basis.HEURISTIC),
        (f"{PY}/users/schemas.py#UserOut", "schema", Basis.HEURISTIC),
        ("backend/tests/test_users.py", "test_file", Basis.HEURISTIC),
        ("backend/tests/test_users.py#test_get_user_returns_none_when_missing",
         "test_function", Basis.HEURISTIC),
    }  # fmt: skip


def test_origins_and_exclusion_policy(golden: SnapshotDocument) -> None:
    origins = {c.node_key: c.value for c in golden.classifications if c.facet == "origin"}
    assert origins["README.md"] == "docs"
    assert origins[f"{PY}/generated/user_pb2.py"] == "generated"
    assert origins["frontend/vendor"] == origins["frontend/vendor/leftpad.js"] == "vendored"
    assert {v for k, v in origins.items() if k.endswith(".py") and "generated" not in k} == {
        "source"
    }
    # Vendored and docs: file node only. Generated: extracted, but marked.
    assert golden.node("frontend/vendor/leftpad.js").extraction_status is ExtractionStatus.EXCLUDED
    assert golden.node("README.md").extraction_status is ExtractionStatus.EXCLUDED
    assert golden.children("frontend/vendor/leftpad.js") == ()
    generated = golden.node(f"{PY}/generated/user_pb2.py")
    assert generated.extraction_status is ExtractionStatus.FULL
    assert [n.key for n in golden.children(generated.key)] == [f"{generated.key}#UserMessage"]


def test_unresolved_references(golden: SnapshotDocument) -> None:
    refs = {(u.source_key, u.raw_text, u.reason) for u in golden.unresolved_references}
    assert refs == {
        (f"{PY}/plugins.py", "acme_legacy_billing", UnresolvedReason.NOT_FOUND),
        (f"{PY}/plugins.py#load_plugin", "importlib.import_module(name)", UnresolvedReason.DYNAMIC),
    }
    # An unresolved import produces no edge and no invented node.
    assert not any("acme_legacy_billing" in n.key for n in golden.nodes)


def test_coverage_gaps_are_explicit(golden: SnapshotDocument) -> None:
    assert golden.node("backend/app/legacy.py").extraction_status is ExtractionStatus.FAILED
    assert [i.code for i in golden.extraction_issues] == ["parse_error"]
    assert golden.node("scripts/deploy.sh").extraction_status is ExtractionStatus.UNSUPPORTED
    # CALLS is not extracted in this snapshot, so the absence of CALLS edges means nothing.
    declared = {k for c in golden.coverage for k in c.edge_kinds}
    assert EdgeKind.CALLS not in declared
    assert not golden.edges_of(EdgeKind.CALLS)


def test_snapshot_tier_holds_no_inferred_or_ai_claims(golden: SnapshotDocument) -> None:
    bases = {n.basis for n in golden.nodes} | {e.basis for e in golden.edges}
    bases |= {c.basis for c in golden.classifications}
    assert bases <= {Basis.OBSERVED, Basis.RESOLVED, Basis.HEURISTIC}
