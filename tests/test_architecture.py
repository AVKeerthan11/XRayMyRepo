"""Layering rules, checked on imports:

cim <- analyzer
cim <- graph                        (pure shared building blocks)
cim + graph <- architecture         (pure derivation: CIM in, CIM out)
cim + graph <- insights             (pure derivation: CIM in, CIM out)
cim <- persistence <- api           (the API only reads, through persistence; it may also
                                     run the pure insights/graph code on request)
architecture + insights + persistence <- orchestration (load -> derive -> persist)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .conftest import ROOT

PACKAGE = ROOT / "src" / "xraymyrepo"

_DERIVATION = {"xraymyrepo.architecture", "xraymyrepo.insights"}
_UPPER = {*_DERIVATION, "xraymyrepo.graph", "xraymyrepo.orchestration"}
_IO = {"xraymyrepo.persistence", "xraymyrepo.api", "psycopg", "fastapi", "starlette"}

# Imports (top-level module names or xraymyrepo subpackages) each layer must not use.
FORBIDDEN = {
    "cim": {"xraymyrepo.analyzer", *_UPPER, *_IO},
    "analyzer": {*_UPPER, *_IO},
    "graph": {"xraymyrepo.analyzer", *(_UPPER - {"xraymyrepo.graph"}), *_IO},
    # The two derivation packages do not depend on each other.
    "architecture": {"xraymyrepo.analyzer", "xraymyrepo.insights", "xraymyrepo.orchestration",
                     *_IO},
    "insights": {"xraymyrepo.analyzer", "xraymyrepo.architecture", "xraymyrepo.orchestration",
                 *_IO},
    "persistence": {"xraymyrepo.analyzer", "xraymyrepo.api", *_UPPER, "fastapi", "starlette"},
    # On-request computations (change impact) use the pure insights and graph code; the
    # API never derives-and-stores (orchestration) or touches the architecture algorithm.
    "api": {"xraymyrepo.analyzer", "xraymyrepo.architecture", "xraymyrepo.orchestration"},
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
    assert {"xraymyrepo.architecture", "xraymyrepo.insights", "xraymyrepo.persistence"} <= names
    for forbidden in ("xraymyrepo.api", "xraymyrepo.analyzer", "fastapi"):
        assert not any(n == forbidden or n.startswith(forbidden + ".") for n in names)
