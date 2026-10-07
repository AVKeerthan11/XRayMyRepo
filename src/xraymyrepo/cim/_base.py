"""Shared model base and small value grammars used across the CIM contract."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

# Every CIM record is an immutable value. Collections are tuples, so frozen models
# are deeply immutable (the one dict, AnalysisConfig.exclusion_policy, is hashed
# into the snapshot identity and must be treated as read-only).
_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class CIMModel(BaseModel):
    model_config = _MODEL_CONFIG


# Maximum evidence items kept per record. The full count lives in
# ``occurrence_count``; evidence is a sample, primary occurrence first.
MAX_EVIDENCE_SAMPLE = 20

_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_RULE_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _check(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def validate(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"invalid {what}: {value!r}")
        return value

    return AfterValidator(validate)


def _no_control_chars(value: str) -> str:
    if not value or value != value.strip() or _CONTROL.search(value):
        raise ValueError(
            f"must be non-empty, without surrounding whitespace or control chars: {value!r}"
        )
    return value


Slug = Annotated[str, _check(_SLUG, "slug")]
"""Lower-case identifier, e.g. ``python``, ``python-extractor``, ``domain``."""

RuleId = Annotated[str, _check(_RULE_ID, "rule id")]
"""Dotted rule identifier from a producer's rule catalog, e.g. ``py.import.relative``."""

SemVer = Annotated[str, _check(_SEMVER, "semantic version")]

Sha256 = Annotated[str, _check(_SHA256, "sha256 digest (expected 'sha256:<64 hex>')")]

GitOid = Annotated[str, _check(_GIT_OID, "git object id (40 or 64 lower-case hex)")]

Text = Annotated[str, AfterValidator(_no_control_chars)]
"""Single-line, non-empty human-readable text."""


def is_slug(value: str) -> bool:
    return bool(_SLUG.fullmatch(value))
