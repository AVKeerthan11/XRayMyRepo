"""Node key grammar: the stable, snapshot-independent *declared identity* of a node.

A key says where something is declared and what it is called. It never contains
line numbers, the snapshot id, or (with one documented exception, ``~type``) the
node kind. Keys change when code moves or is renamed; following an entity across
such changes is the job of lineage matching (``lineage.py``), not of the key.

Grammar (see docs/cim/CIM-v1.md, "Identity"):

    repository        /
    directory, file   <path>                       src/app/users/service.py
    type, function    <path>#<segment>(.<segment>)* src/app/users/service.py#UserService.create
    segment           <name>[~type][@<ordinal>]    Foo~type, f@2
    endpoint          endpoint:<protocol>:<name>@<scope>
                      endpoint:http:GET /users/{id}@pkg:npm:@acme/api
    scope             <package key> | /            nearest enclosing package, else the repository
    package           pkg:<ecosystem>:<name>       pkg:npm:@acme/web
    external_package  ext:<ecosystem>:<name>       ext:npm:react
    group             group:<lens>:<local>         group:directory:src/app

Paths are POSIX, relative to the repository root, NFC-normalized, case preserved.
In keys, the three characters that would make the grammar ambiguous are
percent-escaped in paths: ``%`` -> ``%25``, ``#`` -> ``%23``, ``:`` -> ``%3A``.
Therefore a path key never contains ``:`` and cannot collide with a prefixed key.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from ._base import is_slug
from .enums import EndpointProtocol, NodeKind

REPOSITORY_KEY = "/"

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"})

_ESCAPES = {"%": "%25", "#": "%23", ":": "%3A"}
_UNESCAPES = {v: k for k, v in _ESCAPES.items()}
_ESCAPE_RE = re.compile(r"%(?:25|23|3A)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SEGMENT_RE = re.compile(
    r"^(?P<name>[^\s#.~@/:()%]+)(?:~(?P<space>type))?(?:@(?P<ordinal>[2-9]|[1-9][0-9]+))?$"
)
_HTTP_NAME_RE = re.compile(r"^(?P<method>[A-Z]+) (?P<path>/\S*)$")
_EXPRESS_PARAM_RE = re.compile(r"(?<=/):([A-Za-z_][A-Za-z0-9_]*)")
_PREFIXES = ("endpoint:", "pkg:", "ext:", "group:")


class InvalidKeyError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Paths


def encode_path(path: str) -> str:
    """Turn a repository-relative POSIX path into a path key."""
    path = unicodedata.normalize("NFC", path)
    _validate_raw_path(path)
    return "".join(_ESCAPES.get(ch, ch) for ch in path)


def decode_path(path_key: str) -> str:
    _validate_path_key(path_key)
    return _ESCAPE_RE.sub(lambda m: _UNESCAPES[m.group(0)], path_key)


def path_key(path: str) -> str:
    return encode_path(path)


def parent_path_key(key: str) -> str:
    """Key of the containing directory, or the repository key for top-level entries."""
    _validate_path_key(key)
    head, sep, _ = key.rpartition("/")
    return head if sep else REPOSITORY_KEY


def _validate_raw_path(path: str) -> None:
    if not path:
        raise InvalidKeyError("path must not be empty")
    if path.startswith("/"):
        raise InvalidKeyError(f"path must be repository-relative: {path!r}")
    if "\\" in path:
        raise InvalidKeyError(f"path must use POSIX separators: {path!r}")
    if _CONTROL.search(path):
        raise InvalidKeyError(f"path contains control characters: {path!r}")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise InvalidKeyError(f"path has an empty, '.' or '..' segment: {path!r}")


def _validate_path_key(key: str) -> None:
    if key != unicodedata.normalize("NFC", key):
        raise InvalidKeyError(f"key is not NFC-normalized: {key!r}")
    if "#" in key or ":" in key:
        raise InvalidKeyError(f"path key contains unescaped '#' or ':': {key!r}")
    if "%" in _ESCAPE_RE.sub("", key):
        raise InvalidKeyError(f"path key contains an invalid '%' escape: {key!r}")
    _validate_raw_path(key)


# ---------------------------------------------------------------------------
# Symbols


@dataclass(frozen=True)
class Segment:
    """One step of a qualified name: ``name``, plus disambiguators when needed.

    ``space="type"`` marks a type-space declaration that shares its name with a
    value-space declaration in the same scope (e.g. TS ``interface Foo`` next to
    ``const Foo``). ``ordinal`` (>= 2) numbers repeated declarations of the same
    name in the same scope in declaration order; the first one has no ordinal.
    """

    name: str
    space: Literal["type"] | None = None
    ordinal: int | None = None

    def __post_init__(self) -> None:
        if self.ordinal is not None and self.ordinal < 2:
            raise InvalidKeyError("ordinal must be >= 2 (the first declaration has none)")
        if not _SEGMENT_RE.fullmatch(str(self)):
            raise InvalidKeyError(f"invalid qualified-name segment: {self.name!r}")

    def __str__(self) -> str:
        text = self.name
        if self.space:
            text += f"~{self.space}"
        if self.ordinal is not None:
            text += f"@{self.ordinal}"
        return text

    @classmethod
    def parse(cls, text: str) -> Segment:
        match = _SEGMENT_RE.fullmatch(text)
        if not match:
            raise InvalidKeyError(f"invalid qualified-name segment: {text!r}")
        ordinal = match.group("ordinal")
        space: Literal["type"] | None = "type" if match.group("space") else None
        return cls(match.group("name"), space, int(ordinal) if ordinal else None)


def symbol_key(path: str, *segments: Segment | str) -> str:
    if not segments:
        raise InvalidKeyError("a symbol key needs at least one segment")
    parsed = [s if isinstance(s, Segment) else Segment(s) for s in segments]
    return f"{encode_path(path)}#{'.'.join(str(s) for s in parsed)}"


@dataclass(frozen=True)
class SymbolKey:
    file_key: str
    segments: tuple[Segment, ...]

    @property
    def name(self) -> str:
        return self.segments[-1].name

    @property
    def parent_key(self) -> str:
        if len(self.segments) == 1:
            return self.file_key
        return f"{self.file_key}#{'.'.join(str(s) for s in self.segments[:-1])}"


def parse_symbol_key(key: str) -> SymbolKey:
    file_part, sep, qualname = key.partition("#")
    if not sep or not qualname:
        raise InvalidKeyError(f"symbol key must be '<path>#<qualified name>': {key!r}")
    if key != unicodedata.normalize("NFC", key):
        raise InvalidKeyError(f"key is not NFC-normalized: {key!r}")
    _validate_path_key(file_part)
    return SymbolKey(file_part, tuple(Segment.parse(s) for s in qualname.split(".")))


# ---------------------------------------------------------------------------
# Prefixed keys
#
# Endpoint identity is semantic: protocol + canonical name, scoped to the nearest
# package boundary (or the repository when no package encloses the registration
# site). It never mentions the handler, so re-pointing a route at a different
# function keeps the endpoint's key.


def normalize_http_route(route: str) -> str:
    """Canonical HTTP route: Express-style ``:id`` params become ``{id}``."""
    if not route.startswith("/") or any(ch.isspace() for ch in route):
        raise InvalidKeyError(f"HTTP route must start with '/' and contain no spaces: {route!r}")
    return _EXPRESS_PARAM_RE.sub(r"{\1}", route)


def endpoint_name(protocol: EndpointProtocol, route: str, method: str | None = None) -> str:
    if protocol is EndpointProtocol.HTTP:
        if method is None or method.upper() not in HTTP_METHODS:
            raise InvalidKeyError(f"HTTP endpoint needs a method in {sorted(HTTP_METHODS)}")
        return f"{method.upper()} {normalize_http_route(route)}"
    if method is not None:
        raise InvalidKeyError(f"only HTTP endpoints have a method, not {protocol}")
    _check_text(route, "endpoint route")
    return route


def endpoint_key(
    protocol: EndpointProtocol, route: str, method: str | None = None, *, scope: str
) -> str:
    """``endpoint:<protocol>:<name>@<scope>``; ``scope`` is a package key or ``/``."""
    _check_endpoint_scope(scope)
    name = endpoint_name(protocol, route, method)
    key = f"endpoint:{protocol}:{name}@{scope}"
    if parse_endpoint_key(key) != EndpointKey(protocol, name, scope):
        raise InvalidKeyError(f"endpoint key does not split back into name and scope: {key!r}")
    return key


@dataclass(frozen=True)
class EndpointKey:
    protocol: EndpointProtocol
    name: str
    """Canonical endpoint name without the scope: ``GET /users/{id}``, ``acme.Users/Get``."""
    scope: str
    """Key of the package that encloses the registration site, or ``/`` (the repository)."""


def parse_endpoint_key(key: str) -> EndpointKey:
    """Split an endpoint key. The scope is a trailing ``@/`` or the last ``@pkg:``."""
    parsed = parse_prefixed_key(key, "endpoint")
    try:
        protocol = EndpointProtocol(parsed.namespace)
    except ValueError:
        raise InvalidKeyError(f"unknown endpoint protocol in {key!r}") from None
    scoped = parsed.name
    if scoped.endswith(f"@{REPOSITORY_KEY}"):
        name, scope = scoped[: -len(REPOSITORY_KEY) - 1], REPOSITORY_KEY
    else:
        at = scoped.rfind("@pkg:")
        if at < 0:
            raise InvalidKeyError(f"endpoint key needs an '@<scope>' suffix: {key!r}")
        name, scope = scoped[:at], scoped[at + 1 :]
    return EndpointKey(protocol, name, scope)


def _check_endpoint_scope(scope: str) -> None:
    if scope != REPOSITORY_KEY:
        validate_key(NodeKind.PACKAGE, scope)


def normalize_package_name(ecosystem: str, name: str) -> str:
    """Ecosystem-specific canonical package name (PyPI names follow PEP 503)."""
    _check_text(name, "package name")
    if any(ch.isspace() for ch in name):
        raise InvalidKeyError(f"package name must not contain whitespace: {name!r}")
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def external_package_key(ecosystem: str, name: str) -> str:
    _check_ecosystem(ecosystem)
    return f"ext:{ecosystem}:{normalize_package_name(ecosystem, name)}"


def package_key(ecosystem: str, name: str) -> str:
    _check_ecosystem(ecosystem)
    return f"pkg:{ecosystem}:{normalize_package_name(ecosystem, name)}"


def group_key(lens: str, local: str) -> str:
    if not is_slug(lens):
        raise InvalidKeyError(f"lens name must be a slug: {lens!r}")
    _check_text(local, "group local name")
    return f"group:{lens}:{local}"


def _check_ecosystem(ecosystem: str) -> None:
    if not is_slug(ecosystem):
        raise InvalidKeyError(f"ecosystem must be a slug: {ecosystem!r}")


def _check_text(value: str, what: str) -> None:
    if not value or value != value.strip() or _CONTROL.search(value):
        raise InvalidKeyError(f"invalid {what}: {value!r}")


# ---------------------------------------------------------------------------
# Kind-directed validation


@dataclass(frozen=True)
class PrefixedKey:
    prefix: str
    namespace: str  # protocol, ecosystem or lens
    name: str


def parse_prefixed_key(key: str, prefix: str) -> PrefixedKey:
    if not key.startswith(f"{prefix}:"):
        raise InvalidKeyError(f"expected a '{prefix}:' key: {key!r}")
    namespace, sep, name = key[len(prefix) + 1 :].partition(":")
    if not sep:
        raise InvalidKeyError(f"expected '{prefix}:<namespace>:<name>': {key!r}")
    return PrefixedKey(prefix, namespace, name)


def validate_key(kind: NodeKind, key: str) -> str:
    """Raise ``InvalidKeyError`` unless ``key`` is well-formed for ``kind``."""
    if key != unicodedata.normalize("NFC", key):
        raise InvalidKeyError(f"key is not NFC-normalized: {key!r}")
    match kind:
        case NodeKind.REPOSITORY:
            if key != REPOSITORY_KEY:
                raise InvalidKeyError(f"repository key must be {REPOSITORY_KEY!r}")
        case NodeKind.DIRECTORY | NodeKind.FILE:
            _validate_path_key(key)
        case NodeKind.TYPE | NodeKind.FUNCTION:
            parse_symbol_key(key)
        case NodeKind.ENDPOINT:
            endpoint = parse_endpoint_key(key)
            _check_endpoint_scope(endpoint.scope)
            if endpoint.protocol is EndpointProtocol.HTTP:
                http = _HTTP_NAME_RE.fullmatch(endpoint.name)
                if not http or http.group("method") not in HTTP_METHODS:
                    raise InvalidKeyError(f"HTTP endpoint key must be 'METHOD /path': {key!r}")
                if normalize_http_route(http.group("path")) != http.group("path"):
                    raise InvalidKeyError(f"HTTP route is not normalized: {key!r}")
            else:
                _check_text(endpoint.name, "endpoint name")
        case NodeKind.PACKAGE | NodeKind.EXTERNAL_PACKAGE:
            prefix = "pkg" if kind is NodeKind.PACKAGE else "ext"
            parsed = parse_prefixed_key(key, prefix)
            _check_ecosystem(parsed.namespace)
            if normalize_package_name(parsed.namespace, parsed.name) != parsed.name:
                raise InvalidKeyError(f"package name is not normalized: {key!r}")
        case NodeKind.GROUP:
            parsed = parse_prefixed_key(key, "group")
            group_key(parsed.namespace, parsed.name)
    return key


def is_prefixed(key: str) -> bool:
    return key.startswith(_PREFIXES)
