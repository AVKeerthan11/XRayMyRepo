"""Stage 3: parse one Python file and extract its symbols and import references.

A bad file never stops the analysis. Failures become extraction issues and an
extraction status:

* unreadable or undecodable source, syntax errors, or a crash while extracting
  declarations -> ``failed`` (no symbols, no references);
* a crash while extracting import references -> ``partial`` (symbols kept).
"""

from __future__ import annotations

import ast
import warnings

from xraymyrepo.cim import ExtractionStatus, IssueSeverity

from ..facts import PYTHON_PRODUCER, FileFact, ImportFact, IssueFact, ParsedPythonFile, Span
from .imports import extract_imports
from .source import SourceDecodeError, SourceText, decode_source
from .symbols import extract_symbols


def parse_python_file(file: FileFact, module_name: str | None) -> ParsedPythonFile:
    def failed(
        code: str, message: str, span: Span | None = None, rule: str = "py.parse"
    ) -> ParsedPythonFile:
        issue = IssueFact(PYTHON_PRODUCER, rule, IssueSeverity.ERROR, code, message, file.key, span)
        return ParsedPythonFile(file.key, ExtractionStatus.FAILED, module_name, issues=(issue,))

    try:
        text = decode_source(file.path.read_bytes())
    except OSError as exc:
        return failed("read_error", f"cannot read file: {exc}")
    except SourceDecodeError as exc:
        return failed("decode_error", f"cannot decode source: {exc}", rule="py.decode")
    source = SourceText(text)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. SyntaxWarning for invalid escapes
            tree = ast.parse(text, filename=file.key, type_comments=False)
    except SyntaxError as exc:
        return failed("parse_error", exc.msg or "invalid syntax", _syntax_error_span(exc))
    except (ValueError, RecursionError, MemoryError) as exc:  # null bytes, too deeply nested
        return failed("parse_error", f"{type(exc).__name__}: {exc}")

    try:
        symbols = extract_symbols(file.key, tree, source)
    except Exception as exc:  # an extractor bug must not lose the rest of the repository
        return failed(
            "extractor_error",
            f"declaration extraction failed: {type(exc).__name__}: {exc}",
            rule="py.declaration",
        )

    imports: tuple[ImportFact, ...] = ()
    issues: tuple[IssueFact, ...] = ()
    status = ExtractionStatus.FULL
    try:
        imports = extract_imports(file.key, tree, source, symbols)
    except Exception as exc:
        status = ExtractionStatus.PARTIAL
        issues = (
            IssueFact(
                PYTHON_PRODUCER,
                "py.import",
                IssueSeverity.ERROR,
                "extractor_error",
                f"import extraction failed: {type(exc).__name__}: {exc}",
                file.key,
            ),
        )
    return ParsedPythonFile(file.key, status, module_name, symbols.symbols, imports, issues)


def _syntax_error_span(exc: SyntaxError) -> Span | None:
    """``SyntaxError`` positions are 1-based character offsets; the end may be missing."""
    if not exc.lineno or exc.offset is None:
        return None
    line = exc.lineno
    start = max(exc.offset - 1, 0)
    end_line = exc.end_lineno or line
    end = (exc.end_offset - 1) if exc.end_offset else start + 1
    if (end_line, end) <= (line, start):
        end_line, end = line, start + 1
    return Span(line, start, end_line, end)
