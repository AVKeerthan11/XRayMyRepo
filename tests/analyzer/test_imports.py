"""Stages 3-4: import extraction and deterministic resolution."""

from __future__ import annotations

from pathlib import Path

from xraymyrepo.cim import Basis, Confidence, ImportsAttributes, NodeKind, UnresolvedReason

from .helpers import imports_from, run, unresolved_in

PROJECT = {"pyproject.toml": "[project]\nname = 'acme'\ndependencies = ['Requests>=2', 'PyYAML']\n"}


def attrs(edge: object) -> ImportsAttributes:
    attributes = getattr(edge, "attributes", None)
    assert isinstance(attributes, ImportsAttributes)
    return attributes


def test_absolute_local_import_targets_the_most_specific_node(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "app/__init__.py": "",
        "app/models.py": "class User: ...\nTABLE = 'users'\n",
        "app/main.py": "from app.models import User, TABLE\nimport app.models\n",
    })  # fmt: skip
    edges = imports_from(doc, "app/main.py")
    assert set(edges) == {"app/models.py#User", "app/models.py"}
    user, module = edges["app/models.py#User"], edges["app/models.py"]
    assert (user.basis, user.confidence) == (Basis.RESOLVED, Confidence.HIGH)
    assert user.evidence[0].provenance.rule == "py.import.absolute"
    assert attrs(user).imported_names == ("User",)
    # TABLE (a variable) and `import app.models` both target the module file: one edge.
    assert module.occurrence_count == 2
    assert attrs(module).specifiers == ("app.models",)
    assert attrs(module).imported_names == ("TABLE",)


def test_relative_imports(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "pkg/__init__.py": "", "pkg/db.py": "class Base: ...\n",
        "pkg/users/__init__.py": "",
        "pkg/users/models.py": (
            "from ..db import Base\nfrom . import service\nfrom .service import *\n"
        ),
        "pkg/users/service.py": "def get(): ...\n",
    })  # fmt: skip
    edges = imports_from(doc, "pkg/users/models.py")
    assert set(edges) == {"pkg/db.py#Base", "pkg/users/service.py"}
    assert edges["pkg/db.py#Base"].evidence[0].provenance.rule == "py.import.relative"
    service = attrs(edges["pkg/users/service.py"])
    assert (service.specifiers, service.imported_names) == ((".", ".service"), ("*", "service"))


def test_relative_import_above_the_repository_is_unresolved(tmp_path: Path) -> None:
    doc = run(tmp_path, {"top.py": "from ... import nothing\n"})
    (ref,) = unresolved_in(doc, "top.py")
    assert (ref.raw_text, ref.reason, ref.provenance.rule) == (
        "...", UnresolvedReason.NOT_FOUND, "py.import.relative")  # fmt: skip


def test_package_imports(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "lib/__init__.py": "def helper(): ...\n",
        "lib/sub.py": "",
        "use.py": "import lib\nfrom lib import sub, helper\n",
    })  # fmt: skip
    edges = imports_from(doc, "use.py")
    assert set(edges) == {"lib/__init__.py", "lib/sub.py", "lib/__init__.py#helper"}
    assert attrs(edges["lib/sub.py"]).imported_names == ("sub",)


def test_reexports_are_resolved_through(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "pkg/__init__.py": "from .service import UserService\nfrom .compat import fetch as load\n",
        "pkg/service.py": "class UserService: ...\n",
        "pkg/compat.py": "from requests import get as fetch\n",
        "main.py": "from pkg import UserService, load\n",
        **PROJECT,
    })  # fmt: skip
    edges = imports_from(doc, "main.py")
    assert set(edges) == {"pkg/service.py#UserService", "ext:pypi:requests"}
    assert attrs(edges["pkg/service.py#UserService"]).via == ("pkg/__init__.py",)
    assert attrs(edges["ext:pypi:requests"]).via == ("pkg/__init__.py", "pkg/compat.py")
    assert "pkg/__init__.py" not in edges  # no edge to the barrel


def test_reexport_cycles_terminate(tmp_path: Path) -> None:
    doc = run(tmp_path, {"a.py": "from b import x\n", "b.py": "from a import x\n"})
    assert set(imports_from(doc, "a.py")) == {"b.py"}
    assert set(imports_from(doc, "b.py")) == {"a.py"}


def test_unresolved_imports_are_never_edges(tmp_path: Path) -> None:
    doc = run(
        tmp_path, {"m.py": "import acme_missing\nfrom nowhere.deep import thing\nimport requests\n"}
    )
    assert imports_from(doc, "m.py") == {}
    refs = {(u.raw_text, u.reason, u.provenance.rule) for u in unresolved_in(doc, "m.py")}
    assert refs == {
        ("acme_missing", UnresolvedReason.NOT_FOUND, "py.import.absolute"),
        ("nowhere.deep", UnresolvedReason.NOT_FOUND, "py.import.absolute"),
        # Third party, but no manifest declares it: not invented.
        ("requests", UnresolvedReason.NOT_FOUND, "py.import.absolute"),
    }
    assert all(n.kind is not NodeKind.EXTERNAL_PACKAGE for n in doc.nodes)


def test_multiple_imports_merge_into_one_edge(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "m.py": (
            "import requests\nfrom requests import get, post\nfrom requests.auth import AuthBase\n"
        ),
        **PROJECT,
    })  # fmt: skip
    (edge,) = imports_from(doc, "m.py").values()
    assert edge.target_key == "ext:pypi:requests"
    assert edge.occurrence_count == 3 and len(edge.evidence) == 3
    assert [e.start_line for e in edge.evidence] == [1, 2, 3]  # type: ignore[union-attr]
    assert attrs(edge).specifiers == ("requests", "requests.auth")
    assert attrs(edge).imported_names == ("AuthBase", "get", "post")


def test_imported_names_are_the_declared_names(tmp_path: Path) -> None:
    doc = run(
        tmp_path, {"a.py": "class Z: ...\nclass A: ...\n", "b.py": "from a import Z as z, A\n"}
    )
    assert attrs(imports_from(doc, "b.py")["a.py#Z"]).imported_names == ("Z",)


def test_stdlib_and_declared_external_packages(tmp_path: Path) -> None:
    doc = run(
        tmp_path, {"m.py": "import os.path\nfrom json import loads\nimport requests\n", **PROJECT}
    )
    edges = imports_from(doc, "m.py")
    assert set(edges) == {"ext:python-stdlib:os", "ext:python-stdlib:json", "ext:pypi:requests"}
    assert edges["ext:python-stdlib:os"].evidence[0].provenance.rule == "py.import.stdlib"
    assert edges["ext:pypi:requests"].evidence[0].provenance.rule == "py.import.external"
    os_node = doc.node("ext:python-stdlib:os")
    assert (os_node.basis, os_node.provenance.rule, len(os_node.evidence)) == (
        Basis.RESOLVED, "py.import.stdlib", 1)  # fmt: skip
    pyyaml = doc.node("ext:pypi:pyyaml")  # declared, never imported: still a node
    assert pyyaml.evidence[0].provenance.rule == "py.manifest.dependency"


def test_module_names_must_match_distribution_names(tmp_path: Path) -> None:
    # v1 limitation: `yaml` is provided by PyYAML, but nothing maps module names to
    # distributions, so the import stays unresolved instead of being guessed.
    doc = run(tmp_path, {"m.py": "import yaml\n", **PROJECT})
    assert imports_from(doc, "m.py") == {}
    assert [u.raw_text for u in unresolved_in(doc, "m.py")] == ["yaml"]


def test_local_modules_shadow_stdlib(tmp_path: Path) -> None:
    doc = run(tmp_path, {"logging.py": "", "m.py": "import logging\n"})
    assert set(imports_from(doc, "m.py")) == {"logging.py"}


def test_src_layout_and_project_roots(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "svc/pyproject.toml": "[project]\nname = 'svc'\n",
        "svc/src/svc/__init__.py": "", "svc/src/svc/api.py": "def handler(): ...\n",
        "svc/tests/test_api.py": "from svc.api import handler\n",
    })  # fmt: skip
    assert set(imports_from(doc, "svc/tests/test_api.py")) == {"svc/src/svc/api.py#handler"}
    assert doc.node("pkg:pypi:svc").attributes.model_dump()["root_key"] == "svc"


def test_dynamic_imports(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "plugins.py": (
            "import importlib\n\n"
            "def load(name):\n"
            "    importlib.import_module('json')\n"
            "    return importlib.import_module(name)\n"
            "__import__(\n    'missing_mod'\n)\n"
        ),
    })  # fmt: skip
    edge = imports_from(doc, "plugins.py#load")["ext:python-stdlib:json"]
    assert attrs(edge).is_dynamic and edge.evidence[0].provenance.rule == "py.import.dynamic_call"
    refs = {(u.source_key, u.raw_text, u.reason) for u in unresolved_in(doc, "plugins.py")}
    assert refs == {
        ("plugins.py#load", "importlib.import_module(name)", UnresolvedReason.DYNAMIC),
        ("plugins.py", "__import__( 'missing_mod' )", UnresolvedReason.NOT_FOUND),
    }


def test_type_checking_imports_are_type_only(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "a.py": "class A: ...\nclass B: ...\n",
        "b.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from a import A\n"
                "else:\n    from a import B\n",
    })  # fmt: skip
    edges = imports_from(doc, "b.py")
    assert attrs(edges["a.py#A"]).is_type_only and not attrs(edges["a.py#B"]).is_type_only


def test_import_source_is_the_innermost_scope(tmp_path: Path) -> None:
    doc = run(
        tmp_path, {"m.py": "class C:\n    import json\n    def f(self):\n        import os\n"}
    )
    assert set(imports_from(doc, "m.py#C")) == {"ext:python-stdlib:json"}
    assert set(imports_from(doc, "m.py#C.f")) == {"ext:python-stdlib:os"}
    assert imports_from(doc, "m.py") == {}


def test_namespace_packages_are_not_targets(tmp_path: Path) -> None:
    doc = run(
        tmp_path, {"ns/mod.py": "", "m.py": "import ns\nimport ns.mod\nfrom ns import mod, gone\n"}
    )
    assert set(imports_from(doc, "m.py")) == {"ns/mod.py"}
    refs = {(u.raw_text, u.provenance.rule) for u in unresolved_in(doc, "m.py")}
    assert refs == {
        ("ns", "py.import.namespace_package"),
        ("ns.gone", "py.import.namespace_package"),
    }


def test_competing_declarations_target_the_module(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "compat.py": "try:\n    def f(): ...\nexcept ImportError:\n    def f(): ...\n",
        "m.py": "from compat import f\n",
    })  # fmt: skip
    assert set(imports_from(doc, "m.py")) == {"compat.py"}


def test_self_imports_do_not_create_self_loops(tmp_path: Path) -> None:
    doc = run(tmp_path, {"pkg/__init__.py": "VERSION = 1\nfrom . import VERSION as V\n"})
    assert imports_from(doc, "pkg/__init__.py") == {}


def test_failed_targets_are_still_files(tmp_path: Path) -> None:
    doc = run(tmp_path, {"broken.py": "def (:\n", "m.py": "from broken import thing\n"})
    assert set(imports_from(doc, "m.py")) == {"broken.py"}


def test_imports_in_failed_files_are_not_extracted(tmp_path: Path) -> None:
    doc = run(tmp_path, {"broken.py": "import os\ndef (:\n"})
    assert doc.edges == () and doc.unresolved_references == ()
