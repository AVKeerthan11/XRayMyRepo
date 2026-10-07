from __future__ import annotations

from pathlib import Path

from xraymyrepo.analyzer import AnalysisOptions, analyze
from xraymyrepo.cim import Edge, EdgeKind, RepositoryRef, SnapshotDocument, UnresolvedReference

REPO = RepositoryRef(host="local", owner="tests", name="sample")
COMMIT = "f" * 40


def write_repo(root: Path, files: dict[str, str | bytes]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    return root


def run(
    root: Path, files: dict[str, str | bytes], options: AnalysisOptions | None = None
) -> SnapshotDocument:
    return analyze(write_repo(root, files), repository=REPO, commit_sha=COMMIT, options=options)


def imports_from(doc: SnapshotDocument, source_key: str) -> dict[str, Edge]:
    return {
        e.target_key: e
        for e in doc.edges
        if e.kind is EdgeKind.IMPORTS and e.source_key == source_key
    }


def unresolved_in(doc: SnapshotDocument, file_key: str) -> list[UnresolvedReference]:
    return [u for u in doc.unresolved_references if u.file_key == file_key]
