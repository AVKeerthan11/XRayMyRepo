"""Analysis options. Everything here is recorded in the snapshot's ``AnalysisConfig``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

from xraymyrepo.cim import AnalysisConfig, ExclusionAction, Origin
from xraymyrepo.cim.snapshot import DEFAULT_EXCLUSION_POLICY

ANALYZER_VERSION = "0.1.0"
"""Version of every producer in this package and the snapshot's extractor version."""

ANALYZER_LANGUAGES: tuple[str, ...] = ("python", "toml")
"""Languages with an extractor: Python source and ``pyproject.toml`` manifests."""

DEFAULT_IGNORE_GLOBS: tuple[str, ...] = tuple(
    sorted(
        f"**/{name}/**"
        for name in (
            # version control
            ".git",
            ".hg",
            ".svn",
            # installed environments and dependencies
            ".venv",
            "venv",
            "node_modules",
            # caches
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            ".nox",
        )
    )
)
"""Paths never recorded, not even as file nodes. Matched against repository-relative
POSIX paths; ``**/`` matches any number of leading directories."""


@dataclass(frozen=True)
class AnalysisOptions:
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS
    exclusion_policy: dict[Origin, ExclusionAction] = field(
        default_factory=lambda: dict(DEFAULT_EXCLUSION_POLICY)
    )

    def config(self) -> AnalysisConfig:
        return AnalysisConfig(
            languages=ANALYZER_LANGUAGES,
            exclusion_policy=self.exclusion_policy,
            ignore_globs=tuple(sorted(set(self.ignore_globs))),
        )

    @cached_property
    def _ignore_patterns(self) -> tuple[re.Pattern[str], ...]:
        return tuple(compile_glob(g) for g in self.ignore_globs)

    def is_ignored(self, path: str, *, is_dir: bool) -> bool:
        """Whether a repository-relative POSIX path is ignored.

        A directory is ignored when everything inside it would be, so ``**/x/**``
        prunes every directory named ``x``.
        """
        candidate = f"{path}/" if is_dir else path
        return any(p.fullmatch(candidate) for p in self._ignore_patterns)


def compile_glob(glob: str) -> re.Pattern[str]:
    """Translate a glob: ``**/`` any leading directories, ``**`` anything, ``*`` and
    ``?`` within one path segment."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))
