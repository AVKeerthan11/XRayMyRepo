"""Stage 4: deterministic Python import resolution (no execution, no network, no AI).

Module lookup, in order (mirroring ``sys.path`` when run from the project root):

1. **relative** (``py.import.relative``): from the importing file's package directory,
   one directory up per extra dot; never above the repository root.
2. **local** (``py.import.absolute``): the import roots of the nearest Python project
   (see ``manifests.py``), else the repository root; the first root containing the
   module wins. In a directory, a regular package (``x/__init__.py``) wins over a
   module (``x.py``, then ``x.pyi``), which wins over a namespace directory.
3. **standard library** (``py.import.stdlib``): the top-level name is in the frozen
   ``STDLIB_MODULES`` list -> ``ext:python-stdlib:<top>``.
4. **declared dependency** (``py.import.external``): the PEP 503-normalized top-level
   name is a dependency of the nearest project's ``pyproject.toml`` -> ``ext:pypi:<name>``.
5. otherwise **unresolved** (``not_found``). Nothing is invented.

The target of a resolved import is the most specific node: for ``from m import n``,
the symbol ``n`` when ``m`` declares exactly one top-level ``n``; the module ``m``
when ``n`` is re-bound there by an import, *resolved through* ``m`` (``via``); the
submodule ``m.n`` when ``m`` is a package; else the module file (``n`` is a variable,
or several declarations compete). Namespace directories are not nodes, so an import
that targets only a namespace directory is unresolved (``py.import.namespace_package``).
Dynamic imports resolve only with a literal absolute module name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from xraymyrepo.cim import (
    REPOSITORY_KEY,
    ExtractionStatus,
    UnresolvedReason,
    external_package_key,
)
from xraymyrepo.cim.keys import normalize_package_name, parent_path_key

from ..facts import (
    FileFact,
    ImportFact,
    ImportForm,
    ParsedPythonFile,
    PythonProjectFact,
    ResolvedImport,
    UnresolvedImport,
)
from ..manifests import nearest_project
from .stdlib import STDLIB_MODULES

RULE_RELATIVE = "py.import.relative"
RULE_ABSOLUTE = "py.import.absolute"
RULE_STDLIB = "py.import.stdlib"
RULE_EXTERNAL = "py.import.external"
RULE_DYNAMIC = "py.import.dynamic_call"
RULE_NAMESPACE = "py.import.namespace_package"
MAX_REEXPORT_DEPTH = 16
_EXTRACTED = (ExtractionStatus.FULL, ExtractionStatus.PARTIAL)


@dataclass(frozen=True)
class _Module:
    """A module found in the repository."""

    file_key: str | None
    """The module's file (``x.py`` or ``x/__init__.py``); None for a namespace directory."""
    dir_key: str | None
    """The package directory when the module is a package."""


@dataclass(frozen=True)
class _External:
    key: str
    rule: str


@dataclass(frozen=True)
class _Missing:
    rule: str


_Lookup = _Module | _External | _Missing


def child(directory: str, name: str) -> str:
    return name if directory == REPOSITORY_KEY else f"{directory}/{name}"


class Resolver:
    def __init__(
        self,
        files: Iterable[FileFact],
        directories: Iterable[str],
        parsed: Iterable[ParsedPythonFile],
        projects: Iterable[PythonProjectFact],
    ) -> None:
        self.files = {f.key for f in files}
        self.directories = {*directories, REPOSITORY_KEY}
        self.projects = tuple(projects)
        self.parsed = {p.file_key: p for p in parsed}
        self.top_symbols: dict[str, dict[str, list[str]]] = {}
        self.bindings: dict[str, dict[str, tuple[ImportFact, str | None]]] = {}
        for p in self.parsed.values():
            symbols: dict[str, list[str]] = {}
            for symbol in p.symbols:
                if symbol.parent_key == p.file_key:
                    symbols.setdefault(symbol.name, []).append(symbol.key)
            self.top_symbols[p.file_key] = symbols
            bound: dict[str, tuple[ImportFact, str | None]] = {}
            for fact in p.imports:
                if fact.source_key != p.file_key:
                    continue  # only module-level imports bind module attributes
                if fact.form is ImportForm.FROM:
                    for name, imported in fact.bindings:
                        bound.setdefault(name, (fact, imported))
                elif fact.form is ImportForm.IMPORT and fact.alias is not None:
                    bound.setdefault(fact.alias, (fact, None))
            self.bindings[p.file_key] = bound

    # -- public --------------------------------------------------------------

    def resolve(self, fact: ImportFact) -> tuple[list[ResolvedImport], list[UnresolvedImport]]:
        if fact.form is ImportForm.DYNAMIC and (fact.module is None or fact.module.startswith(".")):
            call = fact.raw_text or fact.specifier
            return [], [UnresolvedImport(fact, call, UnresolvedReason.DYNAMIC, RULE_DYNAMIC)]
        found = self._module(fact)
        rule = RULE_DYNAMIC if fact.form is ImportForm.DYNAMIC else _rule(found, fact)
        raw = fact.raw_text if fact.form is ImportForm.DYNAMIC else fact.specifier
        assert raw is not None
        if isinstance(found, _Missing):
            return [], [UnresolvedImport(fact, raw, UnresolvedReason.NOT_FOUND, rule)]
        names = tuple(sorted(set(fact.names)))
        if isinstance(found, _External):
            return [ResolvedImport(fact, found.key, rule, names)], []
        if fact.form is not ImportForm.FROM:
            if found.file_key is None:
                return [], [UnresolvedImport(fact, raw, UnresolvedReason.NOT_FOUND, RULE_NAMESPACE)]
            return self._edges(fact, rule, {(found.file_key, ()): []}), []

        targets: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        unresolved: list[UnresolvedImport] = []
        for name in fact.names:
            hit = self._name(found, name, frozenset())
            if hit is None:
                joined = raw + name if raw.endswith(".") else f"{raw}.{name}"
                unresolved.append(
                    UnresolvedImport(fact, joined, UnresolvedReason.NOT_FOUND, RULE_NAMESPACE)
                )
            else:
                targets.setdefault(hit, []).append(name)
        return self._edges(fact, rule, targets), unresolved

    # -- modules -------------------------------------------------------------

    def _module(self, fact: ImportFact) -> _Lookup:
        parts = fact.module.split(".") if fact.module else []
        if fact.level > 0:
            base = parent_path_key(fact.file_key)
            for _ in range(fact.level - 1):
                if base == REPOSITORY_KEY:
                    return _Missing(RULE_RELATIVE)
                base = parent_path_key(base)
            return self._find(base, parts) or _Missing(RULE_RELATIVE)

        project = nearest_project(fact.file_key, self.projects)
        roots = project.source_roots if project is not None else (REPOSITORY_KEY,)
        for root in roots:
            module = self._find(root, parts)
            if module is not None:
                return module
        top = parts[0]
        if top in STDLIB_MODULES:
            return _External(external_package_key("python-stdlib", top), RULE_STDLIB)
        distribution = normalize_package_name("pypi", top.replace("_", "-"))
        if project is not None and distribution in project.declared:
            return _External(external_package_key("pypi", distribution), RULE_EXTERNAL)
        return _Missing(RULE_ABSOLUTE)

    def _find(self, base: str, parts: list[str]) -> _Module | None:
        if base not in self.directories:
            return None
        if not parts:
            return self._package(base)
        current = base
        for part in parts[:-1]:
            current = child(current, part)
            if current not in self.directories:
                return None
        last = child(current, parts[-1])
        if last in self.directories and child(last, "__init__.py") in self.files:
            return self._package(last)
        for suffix in (".py", ".pyi"):
            if last + suffix in self.files:
                return _Module(last + suffix, None)
        if last in self.directories:
            return _Module(None, last)  # namespace package
        return None

    def _package(self, directory: str) -> _Module:
        init = child(directory, "__init__.py")
        return _Module(init if init in self.files else None, directory)

    # -- names ---------------------------------------------------------------

    def _name(
        self, module: _Module, name: str, seen: frozenset[tuple[str, str]]
    ) -> tuple[str, tuple[str, ...]] | None:
        """(target key, via) for attribute ``name`` of ``module``; None if not found."""
        file_key = module.file_key
        if file_key is None:  # namespace package: only submodules exist
            sub = self._submodule(module, name)
            return (sub, ()) if sub is not None else None
        if name == "*":
            return file_key, ()
        parsed = self.parsed.get(file_key)
        if parsed is not None and parsed.status in _EXTRACTED:
            declared = self.top_symbols[file_key].get(name, [])
            if len(declared) == 1:
                return declared[0], ()
            if declared:
                return file_key, ()  # several competing declarations: the module is certain
            binding = self.bindings[file_key].get(name)
            if (
                binding is not None
                and (file_key, name) not in seen
                and len(seen) < MAX_REEXPORT_DEPTH
            ):
                through = self._through(binding, seen | {(file_key, name)})
                if through is not None:
                    return through[0], (file_key, *through[1])
                return file_key, ()
        sub = self._submodule(module, name)
        return (sub, ()) if sub is not None else (file_key, ())

    def _through(
        self, binding: tuple[ImportFact, str | None], seen: frozenset[tuple[str, str]]
    ) -> tuple[str, tuple[str, ...]] | None:
        """Resolve a name re-bound by an import, from the re-binding module's perspective."""
        fact, imported = binding
        found = self._module(fact)
        if isinstance(found, _External):
            return found.key, ()
        if isinstance(found, _Missing):
            return None
        if imported is None:  # import a.b as name
            return (found.file_key, ()) if found.file_key is not None else None
        return self._name(found, imported, seen)

    def _submodule(self, module: _Module, name: str) -> str | None:
        if module.dir_key is None or name == "*":
            return None
        sub = self._find(module.dir_key, [name])
        return sub.file_key if sub is not None else None

    # -- results -------------------------------------------------------------

    def _edges(
        self, fact: ImportFact, rule: str, targets: dict[tuple[str, tuple[str, ...]], list[str]]
    ) -> list[ResolvedImport]:
        resolved = []
        for (target, via), names in targets.items():
            if target == fact.source_key:
                continue  # a module importing a name from itself: IMPORTS cannot self-loop
            resolved.append(ResolvedImport(fact, target, rule, tuple(sorted(set(names))), via))
        return resolved


def _rule(found: _Lookup, fact: ImportFact) -> str:
    if isinstance(found, _External | _Missing):
        return found.rule
    return RULE_RELATIVE if fact.level > 0 else RULE_ABSOLUTE
