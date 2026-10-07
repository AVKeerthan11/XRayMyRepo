from __future__ import annotations

import pytest

from xraymyrepo.cim import (
    REPOSITORY_KEY,
    EndpointKey,
    EndpointProtocol,
    InvalidKeyError,
    NodeKind,
    Segment,
    endpoint_key,
    external_package_key,
    group_key,
    package_key,
    parse_endpoint_key,
    path_key,
    symbol_key,
    validate_key,
)
from xraymyrepo.cim.keys import (
    decode_path,
    parent_path_key,
    parse_symbol_key,
)


class TestPathKeys:
    def test_plain_paths_are_their_own_keys(self) -> None:
        assert path_key("src/app/users/service.py") == "src/app/users/service.py"
        assert path_key("src/app") == "src/app"

    def test_grammar_characters_are_escaped_and_reversible(self) -> None:
        raw = "docs/C#/100%:notes.md"
        key = path_key(raw)
        assert key == "docs/C%23/100%25%3Anotes.md"
        assert decode_path(key) == raw
        validate_key(NodeKind.FILE, key)

    def test_path_keys_cannot_collide_with_prefixed_keys(self) -> None:
        assert path_key("ext:npm:react") == "ext%3Anpm%3Areact"

    def test_nfc_normalization(self) -> None:
        decomposed = "café.py"
        assert path_key(decomposed) == "café.py"
        with pytest.raises(InvalidKeyError, match="NFC"):
            validate_key(NodeKind.FILE, decomposed)

    @pytest.mark.parametrize(
        "bad", ["", "/abs/path.py", "src\\app.py", "src//a.py", "src/./a.py", "../a.py", "a\nb"]
    )
    def test_invalid_paths(self, bad: str) -> None:
        with pytest.raises(InvalidKeyError):
            path_key(bad)

    def test_unescaped_or_bad_escapes_rejected(self) -> None:
        for bad in ("a#b.py", "a:b.py", "100%.py", "a%41.py"):
            with pytest.raises(InvalidKeyError):
                validate_key(NodeKind.FILE, bad)

    def test_parent_path(self) -> None:
        assert parent_path_key("src/app/users/service.py") == "src/app/users"
        assert parent_path_key("README.md") == REPOSITORY_KEY

    def test_repository_key(self) -> None:
        validate_key(NodeKind.REPOSITORY, "/")
        with pytest.raises(InvalidKeyError):
            validate_key(NodeKind.REPOSITORY, "github.com/acme/app")


class TestSymbolKeys:
    def test_agreed_example(self) -> None:
        key = symbol_key("src/app/users/service.py", "UserService", "create")
        assert key == "src/app/users/service.py#UserService.create"
        parsed = parse_symbol_key(key)
        assert parsed.file_key == "src/app/users/service.py"
        assert parsed.name == "create"
        assert parsed.parent_key == "src/app/users/service.py#UserService"
        assert parse_symbol_key("a.py#f").parent_key == "a.py"

    def test_no_line_numbers_kind_or_snapshot_in_key(self) -> None:
        key = symbol_key("src/a.py", "f")
        assert key == "src/a.py#f"  # nothing but declared location and name

    def test_type_space_disambiguator(self) -> None:
        key = symbol_key("src/order.ts", Segment("Order", space="type"))
        assert key == "src/order.ts#Order~type"
        assert parse_symbol_key(key).segments[0] == Segment("Order", "type")

    def test_redefinition_ordinal(self) -> None:
        assert symbol_key("a.py", Segment("f", ordinal=2)) == "a.py#f@2"
        assert symbol_key("a.py", Segment("Outer", ordinal=2), "m") == "a.py#Outer@2.m"
        with pytest.raises(InvalidKeyError):
            Segment("f", ordinal=1)  # the first declaration carries no ordinal

    def test_both_disambiguators_in_fixed_order(self) -> None:
        assert str(Segment("Foo", "type", 3)) == "Foo~type@3"
        with pytest.raises(InvalidKeyError):
            parse_symbol_key("a.ts#Foo@3~type")

    @pytest.mark.parametrize(
        "bad", ["a.py", "a.py#", "a.py#f.", "a.py#.f", "a.py#f~value", "a.py#f@1", "a.py#f g"]
    )
    def test_invalid_symbol_keys(self, bad: str) -> None:
        with pytest.raises(InvalidKeyError):
            validate_key(NodeKind.FUNCTION, bad)

    def test_anonymous_functions_have_no_key(self) -> None:
        with pytest.raises(InvalidKeyError):
            symbol_key("a.ts", "<lambda:1>")


class TestPrefixedKeys:
    def test_endpoint_keys(self) -> None:
        key = endpoint_key(EndpointProtocol.HTTP, "/users/{id}", "get", scope="pkg:npm:@acme/api")
        assert key == "endpoint:http:GET /users/{id}@pkg:npm:@acme/api"
        validate_key(NodeKind.ENDPOINT, key)
        assert parse_endpoint_key(key) == EndpointKey(
            EndpointProtocol.HTTP, "GET /users/{id}", "pkg:npm:@acme/api"
        )

    def test_endpoint_scope_disambiguates_packages(self) -> None:
        api = endpoint_key(EndpointProtocol.HTTP, "/health", "GET", scope="pkg:npm:@acme/api")
        web = endpoint_key(EndpointProtocol.HTTP, "/health", "GET", scope="pkg:npm:@acme/web")
        assert api == "endpoint:http:GET /health@pkg:npm:@acme/api"
        assert api != web

    def test_repository_scope_without_a_package(self) -> None:
        key = endpoint_key(EndpointProtocol.HTTP, "/health", "GET", scope="/")
        assert key == "endpoint:http:GET /health@/"
        assert parse_endpoint_key(key).scope == "/"
        validate_key(NodeKind.ENDPOINT, key)

    @pytest.mark.parametrize(
        ("route", "scope"),
        [("/users/@{handle}", "pkg:npm:@acme/api"), ("/a@/", "/"), ("/a@/", "pkg:pypi:x"),
         ("/x@pkg:npm:y", "pkg:npm:z")],
    )  # fmt: skip
    def test_routes_containing_at_signs_split_correctly(self, route: str, scope: str) -> None:
        key = endpoint_key(EndpointProtocol.HTTP, route, "GET", scope=scope)
        assert parse_endpoint_key(key) == EndpointKey(EndpointProtocol.HTTP, f"GET {route}", scope)

    def test_endpoint_scope_must_be_a_package_or_the_repository(self) -> None:
        for bad in ("backend", "ext:npm:react", "pkg:PyPI:x", "pkg:pypi:Not_Normal"):
            with pytest.raises(InvalidKeyError):
                endpoint_key(EndpointProtocol.HTTP, "/x", "GET", scope=bad)

    def test_express_params_are_normalized(self) -> None:
        assert endpoint_key(EndpointProtocol.HTTP, "/orders/:id", "GET", scope="/") == (
            "endpoint:http:GET /orders/{id}@/"
        )
        with pytest.raises(InvalidKeyError, match="normalized"):
            validate_key(NodeKind.ENDPOINT, "endpoint:http:GET /orders/:id@/")

    def test_non_http_endpoints(self) -> None:
        key = endpoint_key(EndpointProtocol.GRPC, "acme.Users/Get", scope="pkg:go:acme/users")
        assert key == "endpoint:grpc:acme.Users/Get@pkg:go:acme/users"
        validate_key(NodeKind.ENDPOINT, key)
        with pytest.raises(InvalidKeyError):
            endpoint_key(EndpointProtocol.CLI, "users create", method="GET", scope="/")

    @pytest.mark.parametrize(
        "bad",
        ["endpoint:http:FETCH /x@/", "endpoint:http:GET x@/", "endpoint:soap:Op@/", "endpoint:http",
         "endpoint:http:GET /x", "endpoint:http:GET /x@backend", "endpoint:http:GET /x@ext:npm:a",
         "endpoint:grpc:@/"],
    )  # fmt: skip
    def test_invalid_endpoint_keys(self, bad: str) -> None:
        with pytest.raises(InvalidKeyError):
            validate_key(NodeKind.ENDPOINT, bad)

    def test_package_keys(self) -> None:
        assert external_package_key("npm", "react") == "ext:npm:react"
        assert package_key("npm", "@acme/web") == "pkg:npm:@acme/web"
        validate_key(NodeKind.PACKAGE, "pkg:npm:@acme/web")

    def test_pypi_names_are_pep503_normalized(self) -> None:
        assert external_package_key("pypi", "SQLAlchemy") == "ext:pypi:sqlalchemy"
        assert external_package_key("pypi", "Zope.Interface_x") == "ext:pypi:zope-interface-x"
        with pytest.raises(InvalidKeyError, match="normalized"):
            validate_key(NodeKind.EXTERNAL_PACKAGE, "ext:pypi:SQLAlchemy")

    def test_versions_are_not_identity(self) -> None:
        with pytest.raises(InvalidKeyError):
            external_package_key("npm", "react 18.2.0")

    def test_group_keys(self) -> None:
        assert group_key("directory", "src/app") == "group:directory:src/app"
        validate_key(NodeKind.GROUP, "group:inferred:cluster-7")
        with pytest.raises(InvalidKeyError):
            validate_key(NodeKind.GROUP, "group:Bad Lens:x")

    def test_kind_directed_validation(self) -> None:
        with pytest.raises(InvalidKeyError):
            validate_key(NodeKind.FILE, "ext:npm:react")
        with pytest.raises(InvalidKeyError):
            validate_key(NodeKind.EXTERNAL_PACKAGE, "pkg:npm:react")
