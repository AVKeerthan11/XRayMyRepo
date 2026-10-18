"""Python fingerprints: what changes ``content_hash`` / ``signature_hash`` and what does not.

Each case analyzes small sources and compares the fingerprints of one declaration.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from xraymyrepo.analyzer import ANALYZER_VERSION
from xraymyrepo.analyzer.python import fingerprint
from xraymyrepo.cim import ExtractionStatus, IssueSeverity, SnapshotDocument, signature_hash
from xraymyrepo.cim.fingerprint import ParameterKind, ParameterShape, SignatureShape

from .helpers import run

_dirs = itertools.count()


def analyzed(tmp_path: Path, source: str | bytes) -> SnapshotDocument:
    return run(tmp_path / f"r{next(_dirs)}", {"m.py": source})


def hashes(tmp_path: Path, source: str | bytes, name: str) -> tuple[str | None, str | None]:
    node = analyzed(tmp_path, source).node(f"m.py#{name}")
    return node.content_hash, node.signature_hash


def content(tmp_path: Path, source: str | bytes, name: str) -> str | None:
    return hashes(tmp_path, source, name)[0]


def signature(tmp_path: Path, source: str | bytes, name: str) -> str | None:
    return hashes(tmp_path, source, name)[1]


BASE = """\
def f(a, b=1):
    if a:
        return a + b
    return b
"""


def test_version() -> None:
    assert ANALYZER_VERSION == "0.2.0"


def test_every_declaration_is_fingerprinted(tmp_path: Path) -> None:
    doc = analyzed(
        tmp_path,
        "class A:\n    def m(self): ...\n    class B: ...\nasync def g(): ...\ntype T = int\n",
    )
    symbols = [n for n in doc.nodes if n.kind.value in ("type", "function")]
    assert len(symbols) == 5
    assert all(n.content_hash and n.signature_hash for n in symbols)
    assert not doc.extraction_issues


# ---------------------------------------------------------------------------
# Content: what does not count


@pytest.mark.parametrize(
    "variant",
    [
        # comments, a trailing comment and blank lines
        "def f(a, b=1):  # doc\n    # why\n\n"
        "    if a:\n        return a + b  # sum\n\n    return b\n",
        # spacing inside lines
        "def f( a,b = 1 ) :\n    if  a :\n        return a+b\n    return   b\n",
        # indentation width and tabs
        "def f(a, b=1):\n  if a:\n    return a + b\n  return b\n",
        "def f(a, b=1):\n\tif a:\n\t\treturn a + b\n\treturn b\n",
        # backslash continuation and brackets spanning lines
        "def f(a, b=1):\n    if a:\n        return a + \\\n            b\n    return b\n",
        "def f(\n    a,\n    b=1):\n    if a:\n        return a + b\n    return b\n",
        # line endings
        "def f(a, b=1):\r\n    if a:\r\n        return a + b\r\n    return b\r\n",
        # what surrounds the declaration
        "import os\n\n\nclass X:\n    def m(self):\n        pass\n\n\ndef f(a, b=1):\n"
        "    if a:\n        return a + b\n    return b\n\nx = 1\n",
    ],
)  # fmt: skip
def test_formatting_does_not_change_content(tmp_path: Path, variant: str) -> None:
    assert content(tmp_path, variant, "f") == content(tmp_path, BASE, "f")


def test_the_declared_name_is_not_content(tmp_path: Path) -> None:
    renamed = BASE.replace("def f", "def renamed")
    assert content(tmp_path, renamed, "renamed") == content(tmp_path, BASE, "f")
    assert signature(tmp_path, renamed, "renamed") == signature(tmp_path, BASE, "f")


def test_uses_of_the_name_in_the_body_are_content(tmp_path: Path) -> None:
    recursive = "def f(n):\n    return f(n - 1)\n"
    renamed = "def g(n):\n    return f(n - 1)\n"
    assert content(tmp_path, recursive, "f") == content(tmp_path, renamed, "g")
    assert content(tmp_path, recursive, "f") != content(
        tmp_path, "def g(n):\n    return g(n - 1)\n", "g"
    )


def test_a_method_does_not_depend_on_its_position(tmp_path: Path) -> None:
    first = "class A:\n    def m(self):\n        return 1\n\n    def other(self): ...\n"
    last = (
        "class A:\n    def other(self):\n        if x:\n            y()\n\n"
        "    def m(self):\n        return 1\n"
    )
    assert content(tmp_path, first, "A.m") == content(tmp_path, last, "A.m")
    assert content(tmp_path, first, "A") != content(tmp_path, last, "A")


# ---------------------------------------------------------------------------
# Content: what counts


@pytest.mark.parametrize(
    "variant",
    [
        # block structure: the last return moved into the if
        "def f(a, b=1):\n    if a:\n        return a + b\n        return b\n",
        # a token
        "def f(a, b=1):\n    if a:\n        return a - b\n    return b\n",
        # a default value (content, not signature)
        "def f(a, b=2):\n    if a:\n        return a + b\n    return b\n",
        # a docstring
        'def f(a, b=1):\n    """Doc."""\n    if a:\n        return a + b\n    return b\n',
        # a decorator
        "@cache\ndef f(a, b=1):\n    if a:\n        return a + b\n    return b\n",
    ],
)
def test_code_changes_change_content(tmp_path: Path, variant: str) -> None:
    assert content(tmp_path, variant, "f") != content(tmp_path, BASE, "f")


def test_string_literals_are_verbatim(tmp_path: Path) -> None:
    single = content(tmp_path, "def f():\n    return 'a  b'\n", "f")
    assert single != content(tmp_path, 'def f():\n    return "a  b"\n', "f")
    assert single != content(tmp_path, "def f():\n    return 'a b'\n", "f")
    assert single != content(tmp_path, "def f():\n    return r'a  b'\n", "f")


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ('f"{x = }"', 'f"{x=}"'),  # the debug specifier prints its whitespace
        ('f"{x}  y"', 'f"{x} y"'),
        ('f"{{a}}"', 'f"{{b}}"'),  # escaped braces (tokenize unescapes them)
        ('f"{x:>{w}}"', 'f"{x:<{w}}"'),
        ("f\"{f'{x}'}\"", "f\"{f'{y}'}\""),  # nested
        ('f"""{x}\n  y"""', 'f"""{x}\n y"""'),  # multi-line
    ],
)
def test_f_strings_are_verbatim(tmp_path: Path, one: str, other: str) -> None:
    def fn(literal: str) -> str:
        return f"def f(x, w):\n    return {literal}\n"

    assert content(tmp_path, fn(one), "f") != content(tmp_path, fn(other), "f")
    assert content(tmp_path, fn(one), "f") == content(
        tmp_path, f"def f(x, w):  # c\n\n    return  {one}\n", "f"
    )


def test_a_method_change_changes_its_class_not_its_siblings(tmp_path: Path) -> None:
    before = "class A:\n    def m(self):\n        return 1\n\n    def n(self):\n        return 2\n"
    after = before.replace("return 1", "return 3")
    assert content(tmp_path, before, "A") != content(tmp_path, after, "A")
    assert content(tmp_path, before, "A.m") != content(tmp_path, after, "A.m")
    assert content(tmp_path, before, "A.n") == content(tmp_path, after, "A.n")


def test_merged_declarations_are_fingerprinted_over_all_parts(tmp_path: Path) -> None:
    prop = (
        "class A:\n    @property\n    def x(self):\n        return self._x\n\n"
        "    @x.setter\n    def x(self, value):\n        self._x = value\n"
    )
    changed_setter = prop.replace("self._x = value", "self._x = int(value)")
    assert content(tmp_path, prop, "A.x") != content(tmp_path, changed_setter, "A.x")
    # The signature is the property's own (the getter).
    assert signature(tmp_path, prop, "A.x") == signature(tmp_path, changed_setter, "A.x")

    overloads = (
        "@overload\ndef f(x: int) -> int: ...\n@overload\ndef f(x: str) -> str: ...\n"
        "def f(x):\n    return x\n"
    )
    changed_stub = overloads.replace("(x: str) -> str", "(x: bytes) -> bytes")
    assert content(tmp_path, overloads, "f") != content(tmp_path, changed_stub, "f")
    # The signature is the implementation's.
    assert signature(tmp_path, overloads, "f") == signature(tmp_path, "def f(x): pass\n", "f")


def test_non_ascii_source(tmp_path: Path) -> None:
    source = "def f():\n    return 'é' + ñ\n\ndef g():\n    return 1\n"
    plain = "def g():\n    return 1\n"
    assert content(tmp_path, source, "g") == content(tmp_path, plain, "g")
    assert content(tmp_path, source, "f") is not None


# ---------------------------------------------------------------------------
# Signatures


def test_signature_shape_of_a_function(tmp_path: Path) -> None:
    source = (
        "async def f(a, /, b: list[ int ], c=1, *args: str, d, e: int = 2, **kw) -> None:\n"
        "    pass\n"
    )
    expected = SignatureShape(
        function_kind="function",
        is_async=True,
        parameters=(
            ParameterShape(name="a", kind=ParameterKind.POSITIONAL_ONLY),
            ParameterShape(name="b", kind=ParameterKind.POSITIONAL, annotation="list[int]"),
            ParameterShape(name="c", kind=ParameterKind.POSITIONAL, has_default=True),
            ParameterShape(name="args", kind=ParameterKind.VAR_POSITIONAL, annotation="str"),
            ParameterShape(name="d", kind=ParameterKind.KEYWORD_ONLY),
            ParameterShape(name="e", kind=ParameterKind.KEYWORD_ONLY, annotation="int",
                           has_default=True),
            ParameterShape(name="kw", kind=ParameterKind.VAR_KEYWORD),
        ),
        returns="None",
    )  # fmt: skip
    assert signature(tmp_path, source, "f") == signature_hash(expected)


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("def f(a: int): pass", "def f(a:int)->None: pass", False),
        ("def f(a: 'X'): pass", 'def f(a: "X"): pass', True),  # unparsed annotations
        ("def f(a=1): pass", "def f(a=2): pass", True),  # default values are not shape
        ("def f(a=1): pass", "def f(a): pass", False),
        ("def f(a): pass", "def f(b): pass", False),  # parameter names are shape
        ("def f(a): pass", "def f(a): return a", True),  # bodies are not shape
        ("def f(a): pass", "async def f(a): pass", False),
        ("def f(*, a): pass", "def f(a): pass", False),
    ],
)
def test_signature_of_functions(tmp_path: Path, one: str, other: str, same: bool) -> None:
    assert (signature(tmp_path, one + "\n", "f") == signature(tmp_path, other + "\n", "f")) is same


def test_signature_of_methods_includes_their_kind(tmp_path: Path) -> None:
    source = "class A:\n    def __init__(self): ...\n    def m(self): ...\n"
    doc = analyzed(tmp_path, source)
    assert doc.node("m.py#A.__init__").signature_hash != doc.node("m.py#A.m").signature_hash


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("class A(B): pass", "class A( B ): x = 1", True),
        ("class A(B): pass", "class A(C): pass", False),
        ("class A(B, C): pass", "class A(C, B): pass", False),
        ("class A[T]: pass", "class A[T: int]: pass", False),
        ("class A(B, metaclass=M): pass", "class A(B): pass", True),  # keywords: not shape
    ],
)
def test_signature_of_classes(tmp_path: Path, one: str, other: str, same: bool) -> None:
    assert (signature(tmp_path, one + "\n", "A") == signature(tmp_path, other + "\n", "A")) is same


def test_type_aliases(tmp_path: Path) -> None:
    plain = hashes(tmp_path, "type P = tuple[int, int]\n", "P")
    generic = hashes(tmp_path, "type P[T] = tuple[T, T]\n", "P")
    other_value = hashes(tmp_path, "type P = tuple[str, str]\n", "P")
    assert plain[1] != generic[1]
    assert plain[1] == other_value[1] and plain[0] != other_value[0]


# ---------------------------------------------------------------------------
# Failures


def test_a_token_content_hash_rejects_leaves_the_node_without_content_hash(
    tmp_path: Path,
) -> None:
    doc = analyzed(tmp_path, "def f():\n    return 'a\x1fb'\n\ndef g(): pass\n")
    f, g = doc.node("m.py#f"), doc.node("m.py#g")
    assert f.content_hash is None and f.signature_hash is not None
    assert g.content_hash is not None
    [issue] = doc.extraction_issues
    assert (issue.code, issue.severity, issue.provenance.rule) == (
        "fingerprint_error",
        IssueSeverity.WARNING,
        "py.fingerprint",
    )
    assert issue.span is not None and issue.span.start_line == 1
    assert doc.node("m.py").extraction_status is ExtractionStatus.FULL


def test_a_tokenizer_failure_keeps_the_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(source: object) -> tuple[fingerprint.Token, ...]:
        raise fingerprint.FingerprintError("cannot tokenize: boom")

    monkeypatch.setattr(fingerprint, "normalized_tokens", broken)
    doc = analyzed(tmp_path, "def f(): pass\nclass A: pass\n")
    assert [(n.content_hash, n.signature_hash is not None) for n in doc.nodes
            if n.key.startswith("m.py#")] == [(None, True), (None, True)]  # fmt: skip
    assert [i.code for i in doc.extraction_issues] == ["fingerprint_error"] * 2
    assert doc.node("m.py").extraction_status is ExtractionStatus.FULL


def test_fingerprints_are_deterministic(tmp_path: Path) -> None:
    source = BASE + "\nclass A(B):\n    @property\n    def x(self): return f'{self}'\n"
    first, second = analyzed(tmp_path, source), analyzed(tmp_path, source)
    assert [(n.key, n.content_hash, n.signature_hash) for n in first.nodes] == [
        (n.key, n.content_hash, n.signature_hash) for n in second.nodes
    ]
