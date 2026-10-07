"""The analyzer pipeline: repository -> facts -> CIM snapshot document.

inventory -> manifests -> Python parsing (symbols, imports) -> import resolution
          -> CIM document construction -> CIM validation (``SnapshotDocument``)
"""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from xraymyrepo.cim import REPOSITORY_KEY, RepositoryRef, SnapshotDocument
from xraymyrepo.cim._base import GitOid
from xraymyrepo.cim.keys import decode_path

from .build import build_document
from .facts import (
    PythonProjectFact,
    RepositoryFacts,
    ResolvedImport,
    UnresolvedImport,
)
from .inventory import scan
from .manifests import nearest_project, read_projects
from .options import AnalysisOptions
from .python.parse import parse_python_file
from .python.resolve import Resolver

_COMMIT: TypeAdapter[str] = TypeAdapter(GitOid)


def analyze(
    root: str | Path,
    *,
    repository: RepositoryRef,
    commit_sha: str,
    options: AnalysisOptions | None = None,
) -> SnapshotDocument:
    """Analyze the working tree at ``root`` and return a validated CIM v1 document.

    ``commit_sha`` is recorded as the snapshot's commit; blob ids are computed from
    the files on disk, so analyze a clean checkout of that commit. Invalid inputs
    raise ``pydantic.ValidationError``; an invalid *output* raises
    ``AnalyzerContractError``.
    """
    options = options or AnalysisOptions()
    _COMMIT.validate_python(commit_sha)  # input errors are reported before any work
    facts = collect_facts(Path(root), options)
    return build_document(facts, repository=repository, commit_sha=commit_sha, options=options)


def collect_facts(root: Path, options: AnalysisOptions) -> RepositoryFacts:
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    inventory = scan(root, options)
    directory_keys = [d.key for d in inventory.directories]
    manifests = read_projects(inventory.files, directory_keys)

    python_files = tuple(
        parse_python_file(f, module_name(f.key, manifests.projects))
        for f in inventory.files
        if f.extracted and f.language == "python"
    )

    resolver = Resolver(inventory.files, directory_keys, python_files, manifests.projects)
    resolved: list[ResolvedImport] = []
    unresolved: list[UnresolvedImport] = []
    for parsed in python_files:
        for fact in parsed.imports:
            found, missing = resolver.resolve(fact)
            resolved += found
            unresolved += missing
    return RepositoryFacts(inventory, manifests, python_files, tuple(resolved), tuple(unresolved))


def module_name(file_key: str, projects: tuple[PythonProjectFact, ...]) -> str | None:
    """Dotted module name relative to the first import root containing the file."""
    project = nearest_project(file_key, projects)
    roots = project.source_roots if project is not None else (REPOSITORY_KEY,)
    for root in roots:
        if root == REPOSITORY_KEY:
            relative = file_key
        elif file_key.startswith(f"{root}/"):
            relative = file_key[len(root) + 1 :]
        else:
            continue
        parts = decode_path(relative).split("/")
        stem = parts[-1].rsplit(".", 1)[0]
        parts = parts[:-1] if stem == "__init__" else [*parts[:-1], stem]
        if parts and all(p.isidentifier() for p in parts):
            return ".".join(parts)
        return None
    return None
