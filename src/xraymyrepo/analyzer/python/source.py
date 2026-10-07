"""Python source text: decoding and position conversion.

``ast`` reports columns as UTF-8 byte offsets into each line; the CIM uses 0-based
Unicode code points. ``SourceText`` converts between the two.
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass, field

from ..facts import Span

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_WHITESPACE = re.compile(r"\s+")


class SourceDecodeError(ValueError):
    pass


def decode_source(data: bytes) -> str:
    """Decode Python source using its PEP 263 declaration (UTF-8 by default, BOM aware)."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (SyntaxError, LookupError, UnicodeDecodeError) as exc:
        raise SourceDecodeError(str(exc)) from exc


@dataclass(frozen=True)
class SourceText:
    text: str
    lines: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(_LINE_BREAK.split(self.text)))

    def line(self, number: int) -> str:
        return self.lines[number - 1] if 1 <= number <= len(self.lines) else ""

    def col(self, line: int, byte_offset: int) -> int:
        """Code-point column of a UTF-8 byte offset on ``line``."""
        encoded = self.line(line).encode("utf-8")
        return len(encoded[:byte_offset].decode("utf-8", errors="ignore"))

    def span(self, line: int, col_offset: int, end_line: int, end_col_offset: int) -> Span:
        return Span(line, self.col(line, col_offset), end_line, self.col(end_line, end_col_offset))

    def segment(self, span: Span) -> str:
        """Source text of a span, whitespace runs collapsed to single spaces."""
        if span.start_line == span.end_line:
            text = self.line(span.start_line)[span.start_col : span.end_col]
        else:
            first = self.line(span.start_line)[span.start_col :]
            middle = self.lines[span.start_line : span.end_line - 1]
            last = self.line(span.end_line)[: span.end_col]
            text = "\n".join((first, *middle, last))
        return _WHITESPACE.sub(" ", text).strip()
