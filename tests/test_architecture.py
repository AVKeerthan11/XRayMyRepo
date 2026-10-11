"""Layering rules, checked on imports:

cim <- analyzer
cim <- architecture                 (pure derivation: CIM in, CIM out)
cim <- persistence <- api           (the API only reads, through persistence)
architecture + persistence <- orchestration (load -> derive -> persist)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .conftest import ROOT

PACKAGE = ROOT / "src" / "xraymyrepo"

_UPPER = {"xraymyrepo.architecture", "xraymyrepo.orchestration"}
_WEB = {"fastapi", "starlette"}

# Imports (top-level module names or xraymyrepo subpackages) each layer must not use.
FORBIDDEN = {
    "cim": {"xraymyrepo.analyzer", "xraymyrepo.persistence", "xraymyrepo.api", "psycopg",
            *_UPPER, *_WEB},
    "analyzer": {"xraymyrepo.persistence", "xraymyrepo.api", "psycopg", *_UPPER, *_WEB},
    "architecture": {"xraymyrepo.analyzer", "xraymyrepo.persistence", "xraymyrepo.api",
                     "xraymyrepo.orchestration", "psycopg", *_WEB},
    "persistence": {"xraymyrepo.analyzer", "xraymyrepo.api", *_UPPER, *_WEB},
    "api": {"xraymyrepo.analyzer", *_UPPER},
}  # fmt: skip


def imports_of(path: Path) -> set[str]:
    package = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts[:-1])
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - node.level + 1]
                names.add(".".join([*base, node.module] if node.module else base))
            elif node.module:
                names.add(node.module)
    return names


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_imports(layer: str) -> None:
    files = sorted((PACKAGE / layer).rglob("*.py"))
    assert files
    for path in files:
        for name in imports_of(path):
            for forbidden in FORBIDDEN[layer]:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.relative_to(ROOT)} imports {name}"
                )


def test_relative_imports_are_resolved() -> None:
    names = imports_of(PACKAGE / "api" / "routes" / "nodes.py")
    assert "xraymyrepo.api.db" in names and "xraymyrepo.api" in names


def test_orchestration_imports() -> None:
    names = imports_of(PACKAGE / "orchestration.py")
    assert {"xraymyrepo.architecture", "xraymyrepo.persistence"} <= names
    for forbidden in ("xraymyrepo.api", "xraymyrepo.analyzer", "fastapi"):
        assert not any(n == forbidden or n.startswith(forbidden + ".") for n in names)
