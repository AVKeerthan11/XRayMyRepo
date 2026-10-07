"""Stage 3: Python declarations, keys, spans and failure handling."""

from __future__ import annotations

from pathlib import Path

from xraymyrepo.cim import (
    ExtractionStatus,
    FunctionKind,
    NodeKind,
    SnapshotDocument,
    TypeKind,
)
from xraymyrepo.cim.keys import parse_symbol_key

from .helpers import run

SERVICE = '''\
import functools
from typing import overload


class UserService:
    """Users."""

    def __init__(self, session):
        self.session = session

    def get(self, user_id: int):
        return self.session.get(user_id)

    @property
    def name(self) -> str:
        return "users"

    @name.setter
    def name(self, value: str) -> None:
        pass

    @functools.cached_property
    def cache(self) -> dict:
        return {}

    @staticmethod
    def build():
        def helper():
            pass
        return helper

    async def fetch(self):
        pass

    class Meta:
        def describe(self):
            pass


@overload
def parse(x: int) -> int: ...
@overload
def parse(x: str) -> str: ...
def parse(x):
    return x


def top():
    pass


type UserId = int
'''


def symbols(doc: SnapshotDocument, file_key: str) -> dict[str, tuple[NodeKind, str | None]]:
    out = {}
    for node in doc.nodes:
        if node.key.startswith(f"{file_key}#"):
            attrs = node.attributes
            kind = getattr(attrs, "function_kind", None) or getattr(attrs, "type_kind", None)
            out[node.key.split("#", 1)[1]] = (node.kind, kind)
    return out


def test_declarations_and_kinds(tmp_path: Path) -> None:
    doc = run(tmp_path, {"svc.py": SERVICE})
    assert symbols(doc, "svc.py") == {
        "UserService": (NodeKind.TYPE, TypeKind.CLASS),
        "UserService.__init__": (NodeKind.FUNCTION, FunctionKind.CONSTRUCTOR),
        "UserService.get": (NodeKind.FUNCTION, FunctionKind.METHOD),
        "UserService.name": (NodeKind.FUNCTION, FunctionKind.ACCESSOR),  # setter merged
        "UserService.cache": (NodeKind.FUNCTION, FunctionKind.ACCESSOR),
        "UserService.build": (NodeKind.FUNCTION, FunctionKind.METHOD),
        "UserService.build.helper": (NodeKind.FUNCTION, FunctionKind.FUNCTION),
        "UserService.fetch": (NodeKind.FUNCTION, FunctionKind.METHOD),
        "UserService.Meta": (NodeKind.TYPE, TypeKind.CLASS),
        "UserService.Meta.describe": (NodeKind.FUNCTION, FunctionKind.METHOD),
        "parse": (NodeKind.FUNCTION, FunctionKind.FUNCTION),  # overloads are one node
        "top": (NodeKind.FUNCTION, FunctionKind.FUNCTION),
        "UserId": (NodeKind.TYPE, TypeKind.TYPE_ALIAS),
    }
    assert getattr(doc.node("svc.py#UserService.fetch").attributes, "is_async", None) is True


def test_keys_mirror_the_declaration_tree(tmp_path: Path) -> None:
    doc = run(tmp_path, {"pkg/svc.py": SERVICE})
    for node in doc.nodes:
        if node.kind in (NodeKind.TYPE, NodeKind.FUNCTION):
            assert node.parent_key == parse_symbol_key(node.key).parent_key
            assert node.language == "python"
            assert node.provenance.producer == "python-extractor"
            assert node.evidence == ()  # observed declarations: span + provenance (as golden)
    assert doc.node("pkg/svc.py").attributes.model_dump() == {"module_name": "pkg.svc"}


def test_no_nodes_for_variables_fields_lambdas_or_decorators(tmp_path: Path) -> None:
    doc = run(tmp_path, {"m.py": "X = 1\nf = lambda: 2\nclass C:\n    y: int = 3\n"})
    assert list(symbols(doc, "m.py")) == ["C"]


def test_repeated_declarations_are_numbered(tmp_path: Path) -> None:
    source = (
        "import sys\nif sys.version_info < (3,):\n    def f(): pass\nelse:\n    def f(): pass\n"
    )
    doc = run(tmp_path, {"compat.py": source})
    assert list(symbols(doc, "compat.py")) == ["f", "f@2"]
    assert doc.node("compat.py#f@2").span.start_line == 5  # type: ignore[union-attr]


def test_spans_include_decorators_and_use_code_points(tmp_path: Path) -> None:
    source = 'def café():\n    return "naïve"\n\n\n@decorate\ndef g():\n    pass\n'
    doc = run(tmp_path, {"u.py": source})
    span = doc.node("u.py#café").span
    assert span is not None
    # "    return "naïve"" is 18 code points (19 UTF-8 bytes).
    assert (span.start_line, span.start_col, span.end_line, span.end_col) == (1, 0, 2, 18)
    g = doc.node("u.py#g").span
    assert g is not None and (g.start_line, g.start_col, g.end_line) == (5, 0, 7)


def test_syntax_errors_fail_one_file_only(tmp_path: Path) -> None:
    doc = run(tmp_path, {"bad.py": "def broken(:\n    pass\n", "good.py": "def ok(): pass\n"})
    assert doc.node("bad.py").extraction_status is ExtractionStatus.FAILED
    assert doc.node("good.py").extraction_status is ExtractionStatus.FULL
    assert "good.py#ok" in doc.nodes_by_key
    (issue,) = doc.extraction_issues
    assert (issue.file_key, issue.code, issue.severity) == ("bad.py", "parse_error", "error")
    assert issue.span is not None and (issue.span.start_line, issue.span.start_col) == (1, 11)
    assert issue.provenance.rule == "py.parse"


def test_undecodable_source_is_a_decode_error(tmp_path: Path) -> None:
    doc = run(tmp_path, {"latin.py": b"x = '\xe9'\n"})
    assert doc.node("latin.py").extraction_status is ExtractionStatus.FAILED
    assert [(i.code, i.provenance.rule) for i in doc.extraction_issues] == [
        ("decode_error", "py.decode")
    ]


def test_declared_encodings_are_honoured(tmp_path: Path) -> None:
    doc = run(tmp_path, {"latin.py": b"# -*- coding: latin-1 -*-\nclass Caf\xe9: pass\n"})
    assert "latin.py#Café" in doc.nodes_by_key


def test_null_bytes_fail_the_file(tmp_path: Path) -> None:
    doc = run(tmp_path, {"nul.py": b"x = 1\x00\n"})
    assert doc.node("nul.py").extraction_status is ExtractionStatus.FAILED
    assert doc.extraction_issues[0].code == "parse_error"


def test_bom_and_crlf_sources(tmp_path: Path) -> None:
    doc = run(tmp_path, {"w.py": b"\xef\xbb\xbfclass A:\r\n    def m(self): pass\r\n"})
    span = doc.node("w.py#A.m").span
    assert span is not None and (span.start_line, span.start_col, span.end_col) == (2, 4, 21)


def test_module_names(tmp_path: Path) -> None:
    doc = run(tmp_path, {
        "svc/pyproject.toml": "[project]\nname = 'svc'\n",
        "svc/src/svc/__init__.py": "", "svc/src/svc/api.py": "", "svc/tests/test_api.py": "",
        "scripts/my-tool.py": "", "top.py": "",
    })  # fmt: skip
    names = {k: getattr(doc.node(k).attributes, "module_name", None) for k in (
        "svc/src/svc/__init__.py", "svc/src/svc/api.py", "svc/tests/test_api.py",
        "scripts/my-tool.py", "top.py")}  # fmt: skip
    assert names == {
        "svc/src/svc/__init__.py": "svc", "svc/src/svc/api.py": "svc.api",
        "svc/tests/test_api.py": "tests.test_api", "scripts/my-tool.py": None, "top.py": "top",
    }  # fmt: skip
