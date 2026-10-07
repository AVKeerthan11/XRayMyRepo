"""Python import references: ``import`` / ``from ... import`` statements and dynamic
import calls (``importlib.import_module(...)``, ``import_module(...)`` imported from
``importlib``, ``__import__(...)``).

Each reference is attributed to the innermost named scope containing it. Decorators,
default values, annotations and base classes belong to the enclosing scope, as in
Python. Imports in the body of ``if TYPE_CHECKING:`` are type-only.
"""

from __future__ import annotations

import ast

from ..facts import ImportFact, ImportForm, Span
from .source import SourceText
from .symbols import SymbolTable


def extract_imports(
    file_key: str, tree: ast.Module, source: SourceText, symbols: SymbolTable
) -> tuple[ImportFact, ...]:
    visitor = _ImportVisitor(file_key, source, symbols, _imports_import_module(tree))
    visitor.visit(tree)
    return tuple(visitor.facts)


class _ImportVisitor(ast.NodeVisitor):
    def __init__(
        self, file_key: str, source: SourceText, symbols: SymbolTable, has_import_module: bool
    ) -> None:
        self.file_key = file_key
        self.source = source
        self.symbols = symbols
        self.has_import_module = has_import_module
        self.scope = file_key
        self.type_only = False
        self.facts: list[ImportFact] = []

    # -- scopes --------------------------------------------------------------

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer = [*node.decorator_list]
        if isinstance(node, ast.ClassDef):
            outer += [*node.bases, *(k.value for k in node.keywords)]
        else:
            args = node.args
            outer += [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
            outer += [
                a.annotation
                for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                if a.annotation is not None
            ]
            outer += [
                a.annotation
                for a in (args.vararg, args.kwarg)
                if a is not None and a.annotation is not None
            ]
            if node.returns is not None:
                outer.append(node.returns)
        for expr in outer:
            self.visit(expr)
        saved = self.scope
        self.scope = self.symbols.scope_keys.get(id(node), saved)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = saved

    visit_ClassDef = _visit_scope
    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        saved = self.type_only
        self.type_only = saved or _is_type_checking(node.test)
        for stmt in node.body:
            self.visit(stmt)
        self.type_only = saved
        for stmt in node.orelse:
            self.visit(stmt)

    # -- references ----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        span = self._span(node)
        for alias in node.names:
            self.facts.append(
                ImportFact(
                    ImportForm.IMPORT,
                    self.file_key,
                    self.scope,
                    span,
                    "import_statement",
                    module=alias.name,
                    alias=alias.asname,
                    is_type_only=self.type_only,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.facts.append(
            ImportFact(
                ImportForm.FROM,
                self.file_key,
                self.scope,
                self._span(node),
                "import_from_statement",
                level=node.level,
                module=node.module,
                names=tuple(a.name for a in node.names),
                bindings=tuple((a.asname or a.name, a.name) for a in node.names if a.name != "*"),
                is_type_only=self.type_only,
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_dynamic_import(node.func):
            span = self._span(node)
            first = node.args[0] if node.args else None
            literal = first.value if isinstance(first, ast.Constant) else None
            module = literal if isinstance(literal, str) and literal else None
            self.facts.append(
                ImportFact(
                    ImportForm.DYNAMIC,
                    self.file_key,
                    self.scope,
                    span,
                    "call",
                    module=module,
                    is_type_only=self.type_only,
                    raw_text=self.source.segment(span) or "<call>",
                )
            )
        self.generic_visit(node)

    def _is_dynamic_import(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute):
            return (
                func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
            )
        if isinstance(func, ast.Name):
            return func.id == "__import__" or (
                func.id == "import_module" and self.has_import_module
            )
        return False

    def _span(self, node: ast.stmt | ast.expr) -> Span:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return self.source.span(node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imports_import_module(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "importlib"
        and any(a.name == "import_module" and a.asname is None for a in node.names)
        for node in ast.walk(tree)
    )
