"""Layering rules: cim <- analyzer, cim <- persistence <- api. Checked on imports."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .conftest import ROOT

PACKAGE = ROOT / "src" / "xraymyrepo"

# Imports (top-level module names or xraymyrepo subpackages) each layer must not use.
FORBIDDEN = {
    "cim": {"xraymyrepo.analyzer", "xraymyrepo.persistence", "xraymyrepo.api", "psycopg",
            "fastapi", "starlette"},
    "analyzer": {"xraymyrepo.persistence", "xraymyrepo.api", "psycopg", "fastapi", "starlette"},
    "persistence": {"xraymyrepo.analyzer", "xraymyrepo.api", "fastapi", "starlette"},
    "api": {"xraymyrepo.analyzer"},
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
