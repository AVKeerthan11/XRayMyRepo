"""Analyzer facts: what each pipeline stage found, before it becomes CIM records.

Facts are plain frozen dataclasses. They use CIM keys (built with ``cim.keys``) so
stages can refer to each other, but they carry no CIM validation of their own:
``build.py`` turns them into contract records and ``SnapshotDocument`` validates
the result. Positions are already converted to the contract's convention
(1-based lines, 0-based code-point columns, exclusive end column).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from xraymyrepo.cim import (
    ExtractionStatus,
    FunctionKind,
    IssueSeverity,
    NodeKind,
    Origin,
    TypeKind,
    UnresolvedReason,
)

STRUCTURE_PRODUCER = "structure-extractor"
PYTHON_PRODUCER = "python-extractor"
MANIFEST_PRODUCER = "manifest-extractor"


@dataclass(frozen=True, order=True)
class Span:
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass(frozen=True)
class IssueFact:
    producer: str
    rule: str
    severity: IssueSeverity
    code: str
    message: str
    file_key: str | None = None
    span: Span | None = None


# ---------------------------------------------------------------------------
# Inventory


@dataclass(frozen=True)
class OriginFact:
    """Why a file or directory has its origin. Exactly one of ``pattern`` / ``span``."""

    origin: Origin
    rule: str
    pattern: str | None = None
    matched_value: str | None = None
    span: Span | None = None
    ast_type: str | None = None


@dataclass(frozen=True)
class DirectoryFact:
    key: str
    origin: OriginFact | None = None
    """Only directories that are themselves origin markers (``vendor/``) have one."""


@dataclass(frozen=True)
class FileFact:
    key: str
    path: Path
    language: str | None
    blob_sha: str
    origin: OriginFact
    extracted: bool
    """False when the exclusion policy makes the file ``file_only``."""


@dataclass(frozen=True)
class Inventory:
    directories: tuple[DirectoryFact, ...]
    files: tuple[FileFact, ...]
    issues: tuple[IssueFact, ...] = ()


# ---------------------------------------------------------------------------
# Manifests


@dataclass(frozen=True)
class DependencyFact:
    name: str
    """PEP 503-normalized distribution name."""
    json_pointer: str
    rule: str


@dataclass(frozen=True)
class PythonProjectFact:
    """A directory with a Python project marker (``pyproject.toml``, ``setup.py``, ...)."""

    root_key: str
    """Directory key of the project, or ``/``."""
    source_roots: tuple[str, ...]
    """Import roots in lookup order (``<root>/src`` before ``<root>`` when it exists)."""
    manifest_key: str | None = None
    """The ``pyproject.toml`` read for metadata, if any."""
    package_name: str | None = None
    """PEP 503-normalized ``[project].name``."""
    dependencies: tuple[DependencyFact, ...] = ()

    @property
    def declared(self) -> frozenset[str]:
        return frozenset(d.name for d in self.dependencies)


@dataclass(frozen=True)
class ManifestResult:
    projects: tuple[PythonProjectFact, ...]
    statuses: dict[str, ExtractionStatus] = field(default_factory=dict)
    """Extraction status of every manifest file that was read."""
    issues: tuple[IssueFact, ...] = ()


# ---------------------------------------------------------------------------
# Python source


@dataclass(frozen=True)
class SymbolFact:
    key: str
    kind: NodeKind
    name: str
    parent_key: str
    span: Span
    rule: str
    type_kind: TypeKind | None = None
    function_kind: FunctionKind | None = None
    is_async: bool = False


class ImportForm(StrEnum):
    IMPORT = "import"  # import a.b [as c]
    FROM = "from"  # from [.]a import b [as c]
    DYNAMIC = "dynamic"  # importlib.import_module(...), __import__(...)


@dataclass(frozen=True)
class ImportFact:
    """One module reference in source. ``import a, b`` is two facts with one span."""

    form: ImportForm
    file_key: str
    source_key: str
    """Innermost named scope containing the reference (the file or a symbol in it)."""
    span: Span
    ast_type: str
    level: int = 0
    """Relative import level (number of leading dots)."""
    module: str | None = None
    """Dotted module after the dots; None for ``from . import x`` or a non-literal call."""
    names: tuple[str, ...] = ()
    """Names imported by ``from`` imports, as declared (``*`` for star imports)."""
    alias: str | None = None
    """Binding of ``import a.b as c`` (only ``import`` form)."""
    bindings: tuple[tuple[str, str], ...] = ()
    """(bound name, imported name) for ``from`` imports."""
    is_type_only: bool = False
    raw_text: str | None = None
    """For dynamic imports: the call as written, whitespace collapsed."""

    @property
    def specifier(self) -> str:
        return "." * self.level + (self.module or "")


@dataclass(frozen=True)
class ParsedPythonFile:
    file_key: str
    status: ExtractionStatus
    module_name: str | None
    symbols: tuple[SymbolFact, ...] = ()
    imports: tuple[ImportFact, ...] = ()
    issues: tuple[IssueFact, ...] = ()


# ---------------------------------------------------------------------------
# Resolution


@dataclass(frozen=True)
class ResolvedImport:
    fact: ImportFact
    target_key: str
    rule: str
    imported_names: tuple[str, ...] = ()
    via: tuple[str, ...] = ()

    @property
    def is_dynamic(self) -> bool:
        return self.fact.form is ImportForm.DYNAMIC


@dataclass(frozen=True)
class UnresolvedImport:
    fact: ImportFact
    raw_text: str
    reason: UnresolvedReason
    rule: str


@dataclass(frozen=True)
class RepositoryFacts:
    """Everything the pipeline found, ready for ``build.build_document``."""

    inventory: Inventory
    manifests: ManifestResult
    python_files: tuple[ParsedPythonFile, ...]
    resolved: tuple[ResolvedImport, ...]
    unresolved: tuple[UnresolvedImport, ...]
