"""Python declarations: classes, functions, methods, constructors and type aliases.

The declaration tree is the syntactic scope tree: a declaration's parent is the
nearest enclosing class or function, or the file. ``if``/``try``/``with``/``for``/
``while``/``match`` blocks are not scopes. Within one scope, repeated declarations
of one name are numbered in source order (``f``, ``f@2``), except:

* ``@overload`` stubs are the same node as the implementation (or as the first
  stub when there is no implementation);
* ``@<prop>.setter`` / ``.getter`` / ``.deleter`` are the same node as the property.

Variables, fields, decorators and lambdas are not nodes. Spans include decorators.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from xraymyrepo.cim import FunctionKind, NodeKind, Segment, TypeKind

from ..facts import Span, SymbolFact
from .source import SourceText

Declaration = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.TypeAlias
_ACCESSOR_DECORATORS = frozenset({"property", "cached_property"})
_ACCESSOR_PARTS = frozenset({"setter", "getter", "deleter"})


@dataclass(frozen=True)
class SymbolTable:
    symbols: tuple[SymbolFact, ...]
    scope_keys: dict[int, str]
    """``id()`` of every class/function AST node -> key of the node it belongs to."""


def extract_symbols(file_key: str, tree: ast.Module, source: SourceText) -> SymbolTable:
    extractor = _Extractor(file_key, source)
    extractor.scope(tree.body, file_key, (), in_class=False)
    return SymbolTable(tuple(extractor.symbols), extractor.scope_keys)


class _Extractor:
    def __init__(self, file_key: str, source: SourceText) -> None:
        self.file_key = file_key
        self.source = source
        self.symbols: list[SymbolFact] = []
        self.scope_keys: dict[int, str] = {}

    def scope(
        self, body: list[ast.stmt], parent_key: str, prefix: tuple[Segment, ...], *, in_class: bool
    ) -> None:
        declarations = list(_declarations(body))
        merged = _merged_into(declarations)
        counts: Counter[str] = Counter()
        for node in declarations:
            if id(node) in merged:
                continue
            name = _name(node)
            counts[name] += 1
            segments = (*prefix, Segment(name, ordinal=counts[name] if counts[name] > 1 else None))
            key = f"{self.file_key}#{'.'.join(str(s) for s in segments)}"
            self.scope_keys[id(node)] = key
            self.symbols.append(self._fact(node, key, parent_key, in_class=in_class))
            if not isinstance(node, ast.TypeAlias):
                self.scope(node.body, key, segments, in_class=isinstance(node, ast.ClassDef))
        # A merged stub or accessor is the surviving node. Its body is not walked for
        # declarations (they would collide with the survivor's); anything found inside
        # it later (imports) is attributed to the survivor.
        for node in declarations:
            if id(node) in merged:
                self.scope_keys[id(node)] = self.scope_keys[merged[id(node)]]

    def _fact(self, node: Declaration, key: str, parent_key: str, *, in_class: bool) -> SymbolFact:
        span = self._span(node)
        name = _name(node)
        if isinstance(node, ast.ClassDef):
            return SymbolFact(
                key,
                NodeKind.TYPE,
                name,
                parent_key,
                span,
                "py.declaration.class",
                type_kind=TypeKind.CLASS,
            )
        if isinstance(node, ast.TypeAlias):
            return SymbolFact(
                key,
                NodeKind.TYPE,
                name,
                parent_key,
                span,
                "py.declaration.type_alias",
                type_kind=TypeKind.TYPE_ALIAS,
            )
        if not in_class:
            kind = FunctionKind.FUNCTION
        elif name == "__init__":
            kind = FunctionKind.CONSTRUCTOR
        elif any(_decorator_name(d) in _ACCESSOR_DECORATORS for d in node.decorator_list):
            kind = FunctionKind.ACCESSOR
        else:
            kind = FunctionKind.METHOD
        return SymbolFact(
            key,
            NodeKind.FUNCTION,
            name,
            parent_key,
            span,
            "py.declaration.function",
            function_kind=kind,
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )

    def _span(self, node: Declaration) -> Span:
        assert node.end_lineno is not None and node.end_col_offset is not None
        line, col = node.lineno, self.source.col(node.lineno, node.col_offset)
        decorators = getattr(node, "decorator_list", [])
        if decorators:
            first = decorators[0]
            line = first.lineno
            col = self.source.col(line, first.col_offset)
            text = self.source.line(line)
            at = text.rfind("@", 0, col + 1)
            col = at if at >= 0 else col
        return Span(
            line, col, node.end_lineno, self.source.col(node.end_lineno, node.end_col_offset)
        )


def _name(node: Declaration) -> str:
    return _alias_name(node) if isinstance(node, ast.TypeAlias) else node.name


def _alias_name(node: ast.TypeAlias) -> str:
    assert isinstance(node.name, ast.Name)
    return node.name.id


def _declarations(body: list[ast.stmt]) -> Iterator[Declaration]:
    """Declarations of one scope, in source order, looking through compound statements."""
    for stmt in body:
        if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.TypeAlias):
            yield stmt
        elif isinstance(stmt, ast.If | ast.For | ast.AsyncFor | ast.While):
            yield from _declarations(stmt.body)
            yield from _declarations(stmt.orelse)
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            yield from _declarations(stmt.body)
        elif isinstance(stmt, ast.Try | ast.TryStar):
            yield from _declarations(stmt.body)
            for handler in stmt.handlers:
                yield from _declarations(handler.body)
            yield from _declarations(stmt.orelse)
            yield from _declarations(stmt.finalbody)
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                yield from _declarations(case.body)


def _merged_into(declarations: list[Declaration]) -> dict[int, int]:
    """Declarations that are not their own node -> the declaration they belong to."""
    merged: dict[int, int] = {}
    functions = [d for d in declarations if isinstance(d, ast.FunctionDef | ast.AsyncFunctionDef)]
    by_name: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for function in functions:
        by_name.setdefault(function.name, []).append(function)
    for group in by_name.values():
        overloads = [
            f for f in group if any(_decorator_name(d) == "overload" for d in f.decorator_list)
        ]
        if overloads:
            implementations = [f for f in group if f not in overloads]
            survivor = implementations[0] if implementations else overloads[0]
            for stub in overloads:
                if stub is not survivor:
                    merged[id(stub)] = id(survivor)
        accessors = [f for f in group if id(f) not in merged and _is_accessor_part(f)]
        properties = [f for f in group if id(f) not in merged and not _is_accessor_part(f)]
        if accessors and properties:
            for part in accessors:
                merged[id(part)] = id(properties[0])
    return merged


def _is_accessor_part(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(d, ast.Attribute)
        and d.attr in _ACCESSOR_PARTS
        and isinstance(d.value, ast.Name)
        and d.value.id == function.name
        for d in function.decorator_list
    )


def _decorator_name(decorator: ast.expr) -> str | None:
    """``x`` for ``@x``, ``@m.x``, ``@x(...)`` and ``@m.x(...)``."""
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    return None
