"""Opaque keyset cursors.

A cursor is the sort key of the last item of a page, as base64url JSON. It is
opaque to clients and carries no authority: a tampered cursor can only move the
page start within data the caller may read anyway, and one that does not decode
to the endpoint's sort key shape is rejected as ``invalid_cursor``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from .errors import ApiError

# Cursor parts bounded like the SQL values they are compared with, so a tampered cursor
# is rejected here (invalid_cursor) instead of failing in the database.
Id = Annotated[int, Field(ge=0, le=2**63 - 1)]
"""A database id (``bigint``)."""
Key = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]
"""A key or other text: PostgreSQL text cannot hold NUL."""


def encode(sort_key: Sequence[Any] | int | str) -> str:
    raw = json.dumps(sort_key, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode[K](cursor: str | None, shape: TypeAdapter[K]) -> K | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        return shape.validate_json(raw, strict=True)
    except (binascii.Error, UnicodeDecodeError, ValueError, ValidationError):
        detail = "The cursor is malformed or belongs to another listing."
        raise ApiError(400, "invalid_cursor", "Invalid cursor", detail) from None


def page[T](
    items: Sequence[T], limit: int, sort_key: Callable[[T], Any]
) -> tuple[list[T], str | None]:
    """Split the ``limit + 1`` rows a query returned into a page and the next cursor."""
    if len(items) <= limit:
        return list(items), None
    kept = list(items[:limit])
    return kept, encode(sort_key(kept[-1]))
