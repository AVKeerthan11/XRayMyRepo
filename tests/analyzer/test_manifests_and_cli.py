"""Stage 2 (pyproject manifests), failure isolation, and the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xraymyrepo.analyzer import AnalyzerContractError
from xraymyrepo.analyzer import build as build_module
from xraymyrepo.analyzer.cli import main
from xraymyrepo.analyzer.python import parse as parse_module
from xraymyrepo.cim import ExtractionStatus, IssueSeverity, NodeKind, SnapshotDocument

from .helpers import COMMIT, run, write_repo

PYPROJECT = """\
[project]
name = "Acme_Backend"
dependencies = ["fastapi>=0.110", "SQLAlchemy[asyncio]>=2.0 ; python_version >= '3.10'"]

[project.optional-dependencies]
dev = ["pytest>=8", "fastapi"]
"""


def test_pyproject_declares_package_and_external_packages(tmp_path: Path) -> None:
    doc = run(tmp_path, {"backend/pyproject.toml": PYPROJECT, "backend/app.py": ""})
    package = doc.node("pkg:pypi:acme-backend")
    assert package.attributes.model_dump() == {
        "ecosystem": "pypi", "package_name": "acme-backend",
        "manifest_key": "backend/pyproject.toml", "root_key": "backend",
    }  # fmt: skip
    (role,) = [c for c in doc.classifications_of("backend/pyproject.toml") if c.facet == "role"]
    assert role.value == "manifest" and role.basis == "observed"
    externals = {n.key: n for n in doc.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert set(externals) == {"ext:pypi:fastapi", "ext:pypi:sqlalchemy", "ext:pypi:pytest"}
    pointers = [getattr(e, "json_pointer", None) for e in externals["ext:pypi:fastapi"].evidence]
    assert pointers == ["/project/dependencies/0", "/project/optional-dependencies/dev/1"]
    assert doc.node("backend/pyproject.toml").extraction_status is ExtractionStatus.FULL
    assert doc.edges == ()  # REQUIRES is not extracted in this milestone


def test_invalid_pyproject_fails_only_that_file(tmp_path: Path) -> None:
    doc = run(tmp_path, {"pyproject.toml": "[project\nname=", "a.py": "def f(): ...\n"})
    assert doc.node("pyproject.toml").extraction_status is ExtractionStatus.FAILED
    (issue,) = doc.extraction_issues
    assert (issue.code, issue.severity, issue.provenance.producer) == (
        "manifest_parse_error", IssueSeverity.ERROR, "manifest-extractor")  # fmt: skip
    assert "a.py#f" in doc.nodes_by_key


def test_pyproject_without_project_table(tmp_path: Path) -> None:
    doc = run(tmp_path, {"pyproject.toml": "[tool.ruff]\nline-length = 100\n", "a.py": ""})
    assert doc.node("pyproject.toml").extraction_status is ExtractionStatus.FULL
    assert not any(n.kind is NodeKind.PACKAGE for n in doc.nodes)


def test_duplicate_package_names_keep_the_first(tmp_path: Path) -> None:
    project = "[project]\nname = 'dup'\n"
    doc = run(tmp_path, {"a/pyproject.toml": project, "b/pyproject.toml": project})
    assert doc.node("pkg:pypi:dup").attributes.model_dump()["root_key"] == "a"
    assert [i.code for i in doc.extraction_issues] == ["duplicate_package"]


def test_import_extraction_crash_leaves_a_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(parse_module, "extract_imports", boom)
    doc = run(tmp_path, {"a.py": "import os\nclass A: ...\n"})
    assert doc.node("a.py").extraction_status is ExtractionStatus.PARTIAL
    assert "a.py#A" in doc.nodes_by_key
    (issue,) = doc.extraction_issues
    assert issue.code == "extractor_error" and "RuntimeError: bug" in issue.message


def test_declaration_extraction_crash_fails_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(parse_module, "extract_symbols", boom)
    doc = run(tmp_path, {"a.py": "class A: ...\n", "b.txt": ""})
    assert doc.node("a.py").extraction_status is ExtractionStatus.FAILED
    assert doc.extraction_issues[0].code == "extractor_error"


def test_contract_violations_are_raised_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_module, "EMPTY_CONFIG_HASH", "md5:nope")
    with pytest.raises(AnalyzerContractError, match="violates CIM v1"):
        run(tmp_path, {"a.py": ""})


def test_cli_writes_a_valid_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = write_repo(tmp_path / "repo", {"m.py": "import json\n"})
    assert main(["analyze", str(repo), "--commit", COMMIT, "--repository", "github.com/o/r"]) == 0
    doc = SnapshotDocument.model_validate_json(capsys.readouterr().out)
    assert doc.snapshot.repository.slug == "github.com/o/r"
    assert doc.snapshot.commit_sha == COMMIT

    out = tmp_path / "cim.json"
    assert main(["analyze", str(repo), "--commit", COMMIT, "-o", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["snapshot"]["repository"] == {"host": "local", "owner": "local", "name": "repo"}


def test_cli_reports_bad_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["analyze", str(tmp_path / "missing"), "--commit", COMMIT]) == 1
    assert main(["analyze", str(tmp_path), "--commit", COMMIT, "--repository", "nope"]) == 1
    assert main(["analyze", str(tmp_path), "--commit", "HEAD"]) == 1
    errors = capsys.readouterr().err
    assert "not a directory" in errors and "HOST/OWNER/NAME" in errors
