"""Fingerprints: content identity independent of the key.

* ``content_hash`` identifies a symbol's body. Language extractors produce a
  normalized token stream (no whitespace, no comments) and pass it here; how a
  language tokenizes is the extractor's job and is versioned with the producer.
* ``signature_hash`` identifies a symbol's *shape* without its name, so a renamed
  but otherwise identical function keeps its signature hash.

Both are used by lineage matching; neither is part of the key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum

from ._base import CIMModel, Text
from .enums import FunctionKind, TypeKind

_TOKEN_SEPARATOR = "\x1f"  # ASCII unit separator: cannot occur inside a token


def content_hash(tokens: Iterable[str]) -> str:
    """Hash of an already-normalized token stream."""
    digest = hashlib.sha256()
    for index, token in enumerate(tokens):
        if not token or _TOKEN_SEPARATOR in token:
            raise ValueError(f"invalid token: {token!r}")
        if index:
            digest.update(_TOKEN_SEPARATOR.encode())
        digest.update(token.encode("utf-8"))
    return "sha256:" + digest.hexdigest()


class ParameterKind(StrEnum):
    POSITIONAL_ONLY = "positional_only"
    POSITIONAL = "positional"
    KEYWORD_ONLY = "keyword_only"
    VAR_POSITIONAL = "var_positional"
    VAR_KEYWORD = "var_keyword"


class ParameterShape(CIMModel):
    name: Text
    kind: ParameterKind
    annotation: Text | None = None
    has_default: bool = False


class SignatureShape(CIMModel):
    """Shape of a callable. The callable's own name is deliberately absent."""

    function_kind: FunctionKind
    is_async: bool = False
    parameters: tuple[ParameterShape, ...] = ()
    returns: Text | None = None


class TypeShape(CIMModel):
    """Shape of a type declaration. The type's own name is deliberately absent."""

    type_kind: TypeKind
    bases: tuple[Text, ...] = ()
    type_parameters: tuple[Text, ...] = ()


def signature_hash(shape: SignatureShape | TypeShape) -> str:
    payload = {"shape": type(shape).__name__, **shape.model_dump(mode="json")}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
