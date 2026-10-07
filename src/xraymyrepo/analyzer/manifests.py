"""Stage 2: Python project manifests.

A directory containing ``pyproject.toml``, ``setup.py`` or ``setup.cfg`` is a Python
project root. Its import roots are ``<root>/src`` (when that directory exists)
followed by ``<root>``. Files outside every project use the repository root.

Only ``pyproject.toml`` is read for metadata (PEP 621): ``[project].name`` declares
a package and ``[project].dependencies`` / ``[project.optional-dependencies]``
declare external packages. Other build systems' metadata is not read in v1.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from typing import Any

from xraymyrepo.cim import REPOSITORY_KEY, ExtractionStatus, IssueSeverity
from xraymyrepo.cim.keys import normalize_package_name, parent_path_key

from .facts import (
    MANIFEST_PRODUCER,
    DependencyFact,
    FileFact,
    IssueFact,
    ManifestResult,
    PythonProjectFact,
)

PROJECT_MARKERS = ("pyproject.toml", "setup.cfg", "setup.py")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def read_projects(files: Iterable[FileFact], directory_keys: Iterable[str]) -> ManifestResult:
    directories = set(directory_keys)
    markers: dict[str, list[FileFact]] = {}
    for file in files:
        name = file.key.rsplit("/", 1)[-1]
        if file.extracted and name in PROJECT_MARKERS:
            markers.setdefault(parent_path_key(file.key), []).append(file)

    projects: list[PythonProjectFact] = []
    statuses: dict[str, ExtractionStatus] = {}
    issues: list[IssueFact] = []
    seen_packages: set[str] = set()
    for root in sorted(markers):
        src = "src" if root == REPOSITORY_KEY else f"{root}/src"
        roots = (src, root) if src in directories else (root,)
        pyproject = next((f for f in markers[root] if f.key.endswith("pyproject.toml")), None)
        if pyproject is None:
            projects.append(PythonProjectFact(root, roots))
            continue
        status, project = _read_pyproject(pyproject, root, roots, issues)
        statuses[pyproject.key] = status
        if project.package_name is not None:
            if project.package_name in seen_packages:
                issues.append(
                    _issue(
                        pyproject.key,
                        IssueSeverity.WARNING,
                        "duplicate_package",
                        f"package {project.package_name!r} is declared twice; "
                        "this declaration is ignored",
                    )
                )
                project = PythonProjectFact(root, roots, pyproject.key, None, project.dependencies)
            seen_packages.add(project.package_name or "")
        projects.append(project)
    return ManifestResult(tuple(projects), statuses, tuple(issues))


def nearest_project(
    file_key: str, projects: Iterable[PythonProjectFact]
) -> PythonProjectFact | None:
    by_root = {p.root_key: p for p in projects}
    current = file_key
    while current != REPOSITORY_KEY:
        current = parent_path_key(current)
        if current in by_root:
            return by_root[current]
    return None  # also reached after checking a project rooted at the repository


def _read_pyproject(
    file: FileFact, root: str, roots: tuple[str, ...], issues: list[IssueFact]
) -> tuple[ExtractionStatus, PythonProjectFact]:
    try:
        data = tomllib.loads(file.path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        issues.append(_issue(file.key, IssueSeverity.ERROR, "manifest_parse_error", f"{exc}"))
        return ExtractionStatus.FAILED, PythonProjectFact(root, roots, file.key)
    project = data.get("project")
    if not isinstance(project, dict):
        return ExtractionStatus.FULL, PythonProjectFact(root, roots, file.key)

    name = project.get("name")
    package_name: str | None = None
    if isinstance(name, str) and _REQUIREMENT_NAME.fullmatch(name):
        package_name = normalize_package_name("pypi", name.strip())
    elif name is not None:
        issues.append(
            _issue(
                file.key,
                IssueSeverity.WARNING,
                "invalid_project_name",
                f"[project].name is not a valid name: {name!r}",
            )
        )

    dependencies: list[DependencyFact] = []
    dependencies += _requirements(
        file.key,
        project.get("dependencies"),
        "/project/dependencies",
        "py.manifest.dependency",
        issues,
    )
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for extra in sorted(optional):
            pointer = f"/project/optional-dependencies/{_escape_pointer(extra)}"
            dependencies += _requirements(
                file.key, optional[extra], pointer, "py.manifest.optional_dependency", issues
            )
    return ExtractionStatus.FULL, PythonProjectFact(
        root, roots, file.key, package_name, tuple(dependencies)
    )


def _requirements(
    file_key: str, value: Any, pointer: str, rule: str, issues: list[IssueFact]
) -> list[DependencyFact]:
    if value is None:
        return []
    if not isinstance(value, list):
        issues.append(
            _issue(
                file_key, IssueSeverity.WARNING, "invalid_requirement", f"{pointer} must be a list"
            )
        )
        return []
    found: list[DependencyFact] = []
    for index, requirement in enumerate(value):
        match = _REQUIREMENT_NAME.match(requirement) if isinstance(requirement, str) else None
        if match is None:
            issues.append(
                _issue(
                    file_key,
                    IssueSeverity.WARNING,
                    "invalid_requirement",
                    f"{pointer}/{index} is not a PEP 508 requirement",
                )
            )
            continue
        name = normalize_package_name("pypi", match.group(1))
        found.append(DependencyFact(name, f"{pointer}/{index}", rule))
    return found


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _issue(file_key: str, severity: IssueSeverity, code: str, message: str) -> IssueFact:
    return IssueFact(MANIFEST_PRODUCER, "py.manifest.parse", severity, code, message, file_key)
