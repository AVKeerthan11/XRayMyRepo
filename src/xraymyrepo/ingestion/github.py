"""Pure validation of what a caller asks to ingest: a public GitHub repository URL and
exactly one of a full commit SHA or a ref.

Nothing here touches the filesystem, network, git, the database or the environment.
Inputs are checked as given and never reinterpreted: a URL that is not literally one
of the accepted forms is rejected, not repaired.

Accepted URLs (scheme and host are case-insensitive, nothing else is)::

    https://github.com/OWNER/NAME
    https://github.com/OWNER/NAME/
    https://github.com/OWNER/NAME.git

Owner and name are lower-cased: GitHub treats them case-insensitively, and one
spelling keeps one repository identity in persistence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import TypeAdapter

from xraymyrepo.cim import RepositoryRef
from xraymyrepo.cim._base import GitOid

GITHUB_HOST = "github.com"

_SCHEME = "https://"
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
"""A GitHub account name: 1-39 letters, digits and hyphens, no leading/trailing hyphen."""
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
"""A GitHub repository name: 1-100 letters, digits, ``.``, ``_`` and ``-``."""
_HEX_COMMIT = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_REF_CHARS = re.compile(r"^[\x21-\x7e]+$")
_REF_FORBIDDEN = ("..", "@{", "//", "/.", "~", "^", ":", "?", "*", "[", "\\")
MAX_REF_LENGTH = 255

_COMMIT: TypeAdapter[str] = TypeAdapter(GitOid)


class IngestionInputError(ValueError):
    """A repository URL, commit SHA or ref that cannot be ingested."""


@dataclass(frozen=True)
class IngestionTarget:
    """A validated request to ingest one repository at one commit or ref.

    Exactly one of ``commit_sha`` (full, lower-case) and ``ref`` is set. A ref is
    resolved to a commit SHA by a later stage; the snapshot is always keyed by the SHA.
    """

    repository: RepositoryRef
    commit_sha: str | None = None
    ref: str | None = None


def parse_github_url(url: str) -> RepositoryRef:
    """The repository named by a public GitHub HTTPS URL, owner and name lower-cased.

    Raises ``IngestionInputError`` for anything else: other schemes (including
    SSH-style ``git@github.com:o/r``), other hosts or IP literals, userinfo, ports,
    queries, fragments, percent-encoding, backslashes, whitespace or non-ASCII, extra
    or empty path segments, ``.``/``..`` and invalid owner or repository names. Error
    messages never repeat the URL, which may carry a credential or token.
    """
    if not isinstance(url, str) or not url:
        raise IngestionInputError("repository URL must be a non-empty string")
    if not url.isascii() or not url.isprintable() or any(c.isspace() for c in url):
        raise IngestionInputError("repository URL must be printable ASCII without whitespace")
    if url[: len(_SCHEME)].lower() != _SCHEME:
        raise IngestionInputError("repository URL must start with https://")
    for char, what in (("?", "a query"), ("#", "a fragment"), ("%", "percent-encoding"),
                       ("\\", "backslashes")):  # fmt: skip
        if char in url:
            raise IngestionInputError(f"repository URL must not contain {what}")

    authority, _, path = url[len(_SCHEME) :].partition("/")
    if "@" in authority:
        raise IngestionInputError("repository URL must not contain credentials")
    if authority.startswith("["):
        raise IngestionInputError(f"repository URL host must be {GITHUB_HOST}")
    if ":" in authority:
        raise IngestionInputError("repository URL must not contain a port")
    if authority.lower() != GITHUB_HOST:
        raise IngestionInputError(f"repository URL host must be {GITHUB_HOST}")

    segments = (path[:-1] if path.endswith("/") else path).split("/")
    if len(segments) != 2 or not all(segments):
        raise IngestionInputError("repository URL path must be /OWNER/NAME")
    owner, name = segments
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if owner in (".", "..") or name in ("", ".", ".."):
        raise IngestionInputError("repository URL path must be /OWNER/NAME")
    if not _OWNER.fullmatch(owner):
        raise IngestionInputError(f"invalid GitHub owner: {owner!r}")
    if not _NAME.fullmatch(name):
        raise IngestionInputError(f"invalid GitHub repository name: {name!r}")
    return RepositoryRef(host=GITHUB_HOST, owner=owner.lower(), name=name.lower())


def validate_commit_sha(value: str) -> str:
    """A full commit SHA (40 hex for SHA-1, 64 for SHA-256), lower-cased.

    Abbreviated SHAs are rejected: they can be ambiguous and cannot be fetched.
    """
    if not isinstance(value, str) or not _HEX_COMMIT.fullmatch(value):
        raise IngestionInputError(f"commit SHA must be 40 or 64 hexadecimal characters: {value!r}")
    return _COMMIT.validate_python(value.lower())  # the CIM grammar, as analyze() checks it


def validate_ref(value: str) -> str:
    """A branch, tag or fully qualified ref name, returned unchanged.

    Deliberately stricter than ``git check-ref-format`` and not a full grammar: printable
    ASCII without spaces, at most 255 characters, no leading ``-`` (it could be read as
    a git option) or ``/``, none of ``..``, ``@{``, ``//``, ``~^:?*[\\``, no component
    starting with ``.``, not ending in ``/``, ``.`` or ``.lock``, and not ``@``. A value
    that looks like a full commit SHA is rejected as ambiguous: pass it as the commit.
    """
    if not isinstance(value, str) or not value:
        raise IngestionInputError("ref must be a non-empty string")
    if len(value) > MAX_REF_LENGTH:
        raise IngestionInputError(f"ref must be at most {MAX_REF_LENGTH} characters")
    if not _REF_CHARS.fullmatch(value):
        raise IngestionInputError(
            f"ref must be printable ASCII without spaces or control characters: {value!r}"
        )
    if value.startswith("-"):
        raise IngestionInputError(f"ref must not start with '-': {value!r}")
    if (
        value == "@"
        or value.startswith(("/", "."))
        or value.endswith(("/", ".", ".lock"))
        or any(part in value for part in _REF_FORBIDDEN)
    ):
        raise IngestionInputError(f"invalid ref: {value!r}")
    if _HEX_COMMIT.fullmatch(value):
        raise IngestionInputError(f"ref looks like a commit SHA; pass it as the commit: {value!r}")
    return value


def ingestion_target(
    url: str, *, commit_sha: str | None = None, ref: str | None = None
) -> IngestionTarget:
    """Validate a complete ingestion request: a GitHub URL and exactly one of
    ``commit_sha`` or ``ref`` (an empty string counts as supplied, and is invalid)."""
    if (commit_sha is None) == (ref is None):
        raise IngestionInputError("exactly one of commit SHA or ref is required")
    repository = parse_github_url(url)
    if commit_sha is not None:
        return IngestionTarget(repository, commit_sha=validate_commit_sha(commit_sha))
    assert ref is not None
    return IngestionTarget(repository, ref=validate_ref(ref))
