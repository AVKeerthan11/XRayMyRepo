"""Fingerprints of Python declarations: the CIM ``content_hash`` and ``signature_hash``.

**Content** is the declaration's normalized token stream (``tokenize``, over the whole
decoded file so positions agree with the ``ast`` spans):

* a declaration's tokens are those lying entirely inside its span; a node merged from
  several declarations (``@overload`` stubs, property setters/getters/deleters) is the
  tokens of every part, in source order, separated by ``<DECL>``;
* comments and non-logical line breaks (``COMMENT``, ``NL``) are dropped, so are
  whitespace and backslash continuations (never tokens);
* block structure is kept as fixed markers: ``NEWLINE`` -> ``<NL>``, ``INDENT`` ->
  ``<IN>``, ``DEDENT`` -> ``<DE>`` (indentation width does not matter, nesting does);
  markers before a declaration's first token and after its last are dropped, so the
  hash does not depend on the code around it;
* names, operators, numbers and string literals are kept verbatim (prefix and quotes
  included: ``'a'`` and ``"a"`` differ; docstrings count);
* an f-string (or 3.14 t-string) is one token, its exact source text: whitespace
  inside one can matter (``f"{x = }"``), and ``tokenize`` reports escaped braces
  unescaped;
* the declaration's own name (after ``def``/``class``/``type``) becomes ``<NAME>``,
  so the hash identifies the body independently of the key; uses of the name inside
  the body stay as written.

**Signature** is the existing ``SignatureShape`` / ``TypeShape``: parameters (kind,
name, annotation, whether it has a default; default values are not part of it),
return annotation, function kind and ``async`` for functions; bases and PEP 695 type
parameters for classes; type parameters for ``type`` aliases. Annotations and bases
are ``ast.unparse`` text, so their formatting and comments do not matter.
"""

from __future__ import annotations

import ast
import io
import tokenize
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from xraymyrepo.cim import (
    FunctionKind,
    ParameterShape,
    SignatureShape,
    TypeKind,
    TypeShape,
    content_hash,
    signature_hash,
)
from xraymyrepo.cim.fingerprint import ParameterKind

from ..facts import Span
from .source import SourceText

Declaration = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.TypeAlias
Position = tuple[int, int]

NAME_MARKER = "<NAME>"
PART_SEPARATOR = "<DECL>"
_STRUCTURE = {tokenize.NEWLINE: "<NL>", tokenize.INDENT: "<IN>", tokenize.DEDENT: "<DE>"}
_MARKERS = frozenset(_STRUCTURE.values())
_DROPPED = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER})
# f-strings (3.12+) and t-strings (3.14+) are tokenized into parts; looked up by name
# so either may be absent.
_STRING_STARTS = frozenset(
    t for t, name in tokenize.tok_name.items() if name in ("FSTRING_START", "TSTRING_START")
)
_STRING_ENDS = frozenset(
    t for t, name in tokenize.tok_name.items() if name in ("FSTRING_END", "TSTRING_END")
)
_DECLARING = frozenset({"def", "class", "type"})


class FingerprintError(ValueError):
    """A declaration that cannot be fingerprinted; its node is left without one."""


@dataclass(frozen=True)
class Token:
    text: str
    start: Position
    end: Position

    @property
    def is_marker(self) -> bool:
        return self.text in _MARKERS


def normalized_tokens(source: SourceText) -> tuple[Token, ...]:
    """The file's tokens after normalization, in source order (see the module doc)."""
    tokens: list[Token] = []
    depth = 0
    opened: Position = (0, 0)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source.text, newline=None).readline):
            if tok.type in _STRING_STARTS:
                if depth == 0:
                    opened = tok.start
                depth += 1
            elif tok.type in _STRING_ENDS:
                depth -= 1
                if depth == 0:
                    tokens.append(Token(_source_slice(source, opened, tok.end), opened, tok.end))
            elif depth or tok.type in _DROPPED:
                continue
            elif tok.type in _STRUCTURE:
                tokens.append(Token(_STRUCTURE[tok.type], tok.start, tok.end))
            else:
                tokens.append(Token(tok.string, tok.start, tok.end))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise FingerprintError(f"cannot tokenize: {exc}") from exc
    return tuple(tokens)


def _source_slice(source: SourceText, start: Position, end: Position) -> str:
    """Exact source text between two positions (line breaks normalized to ``\\n``)."""
    (start_line, start_col), (end_line, end_col) = start, end
    if start_line == end_line:
        return source.line(start_line)[start_col:end_col]
    middle = source.lines[start_line : end_line - 1]
    return "\n".join(
        (source.line(start_line)[start_col:], *middle, source.line(end_line)[:end_col])
    )


class Fingerprinter:
    """Fingerprints the declarations of one file. Tokenizes lazily, once."""

    def __init__(self, source: SourceText) -> None:
        self.source = source

    @cached_property
    def _tokens(self) -> tuple[Token, ...]:
        return normalized_tokens(self.source)

    @cached_property
    def _starts(self) -> list[Position]:
        return [t.start for t in self._tokens]

    def content_tokens(self, name: str, spans: Sequence[Span]) -> list[str]:
        """Normalized tokens of a declaration made of the parts at ``spans``."""
        out: list[str] = []
        for index, span in enumerate(sorted(spans, key=lambda s: (s.start_line, s.start_col))):
            if index:
                out.append(PART_SEPARATOR)
            out += _rename(self._within(span), name)
        return out

    def content_hash(self, name: str, spans: Sequence[Span]) -> str:
        try:
            return content_hash(self.content_tokens(name, spans))
        except ValueError as exc:  # FingerprintError, or a token content_hash rejects
            raise FingerprintError(str(exc)) from exc

    def _within(self, span: Span) -> list[Token]:
        first: Position = (span.start_line, span.start_col)
        last: Position = (span.end_line, span.end_col)
        found = []
        for token in self._tokens[bisect_left(self._starts, first) :]:
            if token.start > last:
                break
            if token.end <= last:
                found.append(token)
        while found and found[0].is_marker:
            found.pop(0)
        while found and found[-1].is_marker:
            found.pop()
        return found


def _rename(tokens: list[Token], name: str) -> list[str]:
    """Token texts, the declared name replaced by ``<NAME>``. Decorator lines (``@`` up
    to their ``<NL>``) and ``async`` come before the declaring keyword."""
    texts = [t.text for t in tokens]
    i = 0
    while i < len(texts) and texts[i] == "@":
        while i < len(texts) and texts[i] != "<NL>":
            i += 1
        i += 1
    if i < len(texts) and texts[i] == "async":
        i += 1
    if i + 1 < len(texts) and texts[i] in _DECLARING and texts[i + 1] == name:
        texts[i + 1] = NAME_MARKER
    else:
        raise FingerprintError(f"declaration head of {name!r} not found")
    return texts


# ---------------------------------------------------------------------------
# Signatures


def signature_shape(
    node: Declaration, function_kind: FunctionKind | None = None
) -> SignatureShape | TypeShape:
    if isinstance(node, ast.ClassDef):
        return TypeShape(
            type_kind=TypeKind.CLASS,
            bases=tuple(ast.unparse(b) for b in node.bases),
            type_parameters=tuple(ast.unparse(p) for p in node.type_params),
        )
    if isinstance(node, ast.TypeAlias):
        return TypeShape(
            type_kind=TypeKind.TYPE_ALIAS,
            type_parameters=tuple(ast.unparse(p) for p in node.type_params),
        )
    assert function_kind is not None
    return SignatureShape(
        function_kind=function_kind,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        parameters=_parameters(node.args),
        returns=None if node.returns is None else ast.unparse(node.returns),
    )


def _parameters(args: ast.arguments) -> tuple[ParameterShape, ...]:
    kinds = [ParameterKind.POSITIONAL_ONLY] * len(args.posonlyargs)
    kinds += [ParameterKind.POSITIONAL] * len(args.args)
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    shapes = [
        _parameter(a, kind, has_default=i >= first_default)
        for i, (a, kind) in enumerate(zip(positional, kinds, strict=True))
    ]
    if args.vararg is not None:
        shapes.append(_parameter(args.vararg, ParameterKind.VAR_POSITIONAL))
    shapes += [
        _parameter(a, ParameterKind.KEYWORD_ONLY, has_default=default is not None)
        for a, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    ]
    if args.kwarg is not None:
        shapes.append(_parameter(args.kwarg, ParameterKind.VAR_KEYWORD))
    return tuple(shapes)


def _parameter(arg: ast.arg, kind: ParameterKind, *, has_default: bool = False) -> ParameterShape:
    return ParameterShape(
        name=arg.arg,
        kind=kind,
        annotation=None if arg.annotation is None else ast.unparse(arg.annotation),
        has_default=has_default,
    )


def fingerprint_signature(node: Declaration, function_kind: FunctionKind | None = None) -> str:
    try:
        return signature_hash(signature_shape(node, function_kind))
    except ValueError as exc:  # a shape the contract rejects
        raise FingerprintError(str(exc)) from exc
