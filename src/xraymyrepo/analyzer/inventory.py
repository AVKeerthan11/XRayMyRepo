"""Stage 1: repository inventory.

Walks the working tree in a deterministic order (entries sorted by name), records
directories and files, and decides each file's language, git blob id and origin.
Ignored paths (``AnalysisOptions.ignore_globs``) are not recorded at all. Symbolic
links and Windows junctions are never followed. Directories without any recorded
file are not recorded, matching what git can represent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from xraymyrepo.cim import ExclusionAction, InvalidKeyError, IssueSeverity, path_key

from .facts import STRUCTURE_PRODUCER, DirectoryFact, FileFact, Inventory, IssueFact, OriginFact
from .options import AnalysisOptions
from .origin import directory_marker, file_origin, language_of, marker_fact


def git_blob_sha(data: bytes) -> str:
    """The git object id of ``data`` as a blob (SHA-1 object format)."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def scan(root: Path, options: AnalysisOptions) -> Inventory:
    scanner = _Scanner(root, options)
    scanner.walk(root, ())
    return Inventory(
        directories=tuple(scanner.directories),
        files=tuple(scanner.files),
        issues=tuple(scanner.issues),
    )


class _Scanner:
    def __init__(self, root: Path, options: AnalysisOptions) -> None:
        self.root = root
        self.options = options
        self.directories: list[DirectoryFact] = []
        self.files: list[FileFact] = []
        self.issues: list[IssueFact] = []
        self._packages: dict[tuple[str, ...], bool] = {}

    def walk(self, directory: Path, parts: tuple[str, ...]) -> bool:
        """Record ``directory``'s contents; return whether anything was recorded."""
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            self._issue("unreadable_directory", f"cannot list {'/'.join(parts) or '.'}: {exc}")
            return False
        recorded = False
        for entry in entries:
            rel = "/".join((*parts, entry.name))
            if entry.is_symlink() or entry.is_junction():
                self._issue("symlink_skipped", f"link not followed: {rel}", IssueSeverity.INFO)
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if self.options.is_ignored(rel, is_dir=is_dir):
                continue
            try:
                key = path_key(rel)
            except InvalidKeyError as exc:
                self._issue("invalid_path", f"path cannot be represented as a key: {exc}")
                continue
            if is_dir:
                position = len(self.directories)
                if self.walk(Path(entry.path), (*parts, entry.name)):
                    self.directories.insert(position, self._directory(key, (*parts, entry.name)))
                    recorded = True
            elif entry.is_file(follow_symlinks=False):
                recorded |= self._file(Path(entry.path), key, (*parts, entry.name))
        return recorded

    def _directory(self, key: str, parts: tuple[str, ...]) -> DirectoryFact:
        marker = directory_marker(parts, lambda i: self._is_package(parts[: i + 1]))
        if marker is None or marker[1] != len(parts) - 1:
            return DirectoryFact(key)  # only marker directories carry an origin
        origin, index = marker
        return DirectoryFact(
            key, marker_fact(origin, parts[index], inside=False, matched_value=key)
        )

    def _file(self, path: Path, key: str, parts: tuple[str, ...]) -> bool:
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._issue("unreadable_file", f"cannot read {key}: {exc}", IssueSeverity.ERROR)
            return False
        name = parts[-1]
        language = language_of(name)
        dirs = parts[:-1]
        marker = directory_marker(dirs, lambda i: self._is_package(dirs[: i + 1]))
        marker_origin: OriginFact | None = None
        if marker is not None:
            marker_origin = marker_fact(marker[0], dirs[marker[1]], inside=True, matched_value=key)
        origin = file_origin(key, name, language, data, marker_origin)
        extracted = self.options.exclusion_policy[origin.origin] is ExclusionAction.EXTRACT
        self.files.append(FileFact(key, path, language, git_blob_sha(data), origin, extracted))
        return True

    def _is_package(self, parts: tuple[str, ...]) -> bool:
        if parts not in self._packages:
            self._packages[parts] = (self.root.joinpath(*parts) / "__init__.py").is_file()
        return self._packages[parts]

    def _issue(
        self, code: str, message: str, severity: IssueSeverity = IssueSeverity.WARNING
    ) -> None:
        self.issues.append(IssueFact(STRUCTURE_PRODUCER, "fs.tree", severity, code, message))
