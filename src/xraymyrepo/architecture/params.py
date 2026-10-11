"""Every input of an architecture derivation besides the snapshot and the code version.

Changing any value here changes ``params_hash`` and therefore the derivation run's
identity, so a stored run always states exactly which rules produced it.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xraymyrepo.cim import EdgeKind, Origin, Role
from xraymyrepo.cim._base import Slug

ALGORITHM = "architecture"
ALGORITHM_VERSION = "1.0.0"

DEFAULT_NAME_SIGNALS: dict[str, str] = {
    **dict.fromkeys(
        ("api", "apis", "cli", "controllers", "endpoints", "handlers", "rest", "router",
         "routers", "routes", "schemas", "serializers", "views", "web"),
        "interface",
    ),
    **dict.fromkeys(
        ("application", "service", "services", "use_cases", "usecases"), "application"
    ),
    **dict.fromkeys(("domain", "entities", "entity", "value_objects"), "domain"),
    **dict.fromkeys(
        ("adapters", "dao", "database", "db", "infra", "infrastructure", "migrations",
         "orm", "persistence", "repositories", "repository", "storage"),
        "infrastructure",
    ),
}  # fmt: skip
"""Path segment or file stem -> layer. ``models`` is deliberately absent: whether models
are domain objects or ORM entities is decided by framework imports and roles."""

DEFAULT_FRAMEWORK_SIGNALS: dict[str, str] = {
    **dict.fromkeys(
        ("aiohttp", "argparse", "click", "fastapi", "flask", "starlette", "typer"), "interface"
    ),
    **dict.fromkeys(
        ("alembic", "asyncpg", "boto3", "elasticsearch", "httpx", "peewee", "psycopg",
         "psycopg2", "pymongo", "redis", "requests", "sqlalchemy", "sqlite3"),
        "infrastructure",
    ),
}  # fmt: skip
"""Imported external package name (PyPI or the standard library) -> layer."""

DEFAULT_ROLE_SIGNALS: dict[Role, str] = {
    Role.ENTRYPOINT: "interface",
    Role.SCHEMA: "interface",
    Role.ORM_ENTITY: "infrastructure",
}


class ArchitectureParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layers: tuple[Slug, ...] = ("interface", "application", "domain", "infrastructure")
    """Top to bottom: a layer may depend on itself and on the layers listed after it."""
    extra_allowed: tuple[tuple[Slug, Slug], ...] = (("infrastructure", "domain"),)
    """Upward dependencies that are allowed anyway: (from layer, to layer)."""
    name_signals: dict[str, Slug] = Field(default_factory=lambda: dict(DEFAULT_NAME_SIGNALS))
    framework_signals: dict[str, Slug] = Field(
        default_factory=lambda: dict(DEFAULT_FRAMEWORK_SIGNALS)
    )
    role_signals: dict[Role, Slug] = Field(default_factory=lambda: dict(DEFAULT_ROLE_SIGNALS))
    dependency_kinds: tuple[EdgeKind, ...] = (EdgeKind.CALLS, EdgeKind.IMPORTS, EdgeKind.INHERITS)
    """Edge kinds that mean "source depends on target" (layers, metrics, cycles)."""
    layer_languages: tuple[Slug, ...] = ("python",)
    layer_origins: tuple[Origin, ...] = (Origin.SOURCE,)
    component_languages: tuple[Slug, ...] = ("python",)
    component_origins: tuple[Origin, ...] = (Origin.GENERATED, Origin.SOURCE)
    test_directories: tuple[str, ...] = ("test", "tests")
    """Files under a directory with one of these names are tests (outside layering)."""
    test_file_patterns: tuple[str, ...] = ("*_test.py", "conftest.py", "test_*.py")

    @model_validator(mode="after")
    def _consistent(self) -> ArchitectureParams:
        if len(set(self.layers)) != len(self.layers) or not self.layers:
            raise ValueError("layers must be non-empty and unique")
        known = set(self.layers)
        used = (
            {layer for pair in self.extra_allowed for layer in pair}
            | set(self.name_signals.values())
            | set(self.framework_signals.values())
            | set(self.role_signals.values())
        )
        if not used <= known:
            raise ValueError(f"unknown layers in rule tables: {sorted(used - known)}")
        return self

    def allows(self, source_layer: str, target_layer: str) -> bool:
        """May a file in ``source_layer`` depend on one in ``target_layer``?"""
        order = {layer: i for i, layer in enumerate(self.layers)}
        return (
            order[target_layer] >= order[source_layer]
            or (source_layer, target_layer) in self.extra_allowed
        )

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = self.model_dump(mode="json")
        return data

    def params_hash(self) -> str:
        canonical = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
