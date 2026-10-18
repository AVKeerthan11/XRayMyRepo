"""``xray ingest URL (--ref REF | --commit-sha SHA)``: ingest one commit of a public
GitHub repository as a snapshot in PostgreSQL and print the result as JSON.

**Database.** ``XRAY_DATABASE_URL``, as for the API: a libpq URI or connection string to
a database migrated with ``db/migrations``. It is read only from the environment, never
from the command line (process lists and shell history would expose a password), and it
is never printed.

**Output.** On success, one line of JSON on stdout::

    {"snapshot_id": 7, "repository": {"host": "github.com", "owner": "acme", "name": "app"},
     "commit_sha": "...", "extractor_version": "0.2.0", "config_hash": "sha256:...",
     "already_existed": false, "skipped": [{"path": "docs", "reason": "symlink"}]}

``skipped`` lists the symlinks and submodules the snapshot does not represent; it is
``null`` when the snapshot already existed and nothing was retrieved (unknown, not
none). Skipped entries are also summarized on stderr. On failure stdout is empty and
stderr has one line, ``xray ingest: <code>: <message>``. Messages never contain the
database URL, git output or local temporary paths.

**Exit codes.**

    0  ingested, or the snapshot already existed
    1  unexpected error (only its type is printed)
    2  invalid arguments or input (URL, SHA, ref), or XRAY_DATABASE_URL not set
    3  retrieval failed: git unavailable, repository unavailable or private, ref or
       commit not found, unsafe or unsupported tree
    4  a retrieval limit was exceeded, or retrieval timed out
    5  analysis failed
    6  database error: cannot connect, repository stored with other letter case, an
       incomplete snapshot with this identity, or any other database failure
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any, NoReturn

import psycopg
from pydantic import ValidationError

from xraymyrepo.analyzer import AnalyzerContractError
from xraymyrepo.persistence import PersistenceError
from xraymyrepo.persistence.snapshots import Connection

from .git import RetrievalError, RetrievalLimitError, RetrievalTimeoutError
from .github import IngestionInputError, ingestion_target
from .service import IngestResult, ingest

PROG = "xray ingest"
DATABASE_URL_VARIABLE = "XRAY_DATABASE_URL"
_NOTED_ENTRIES = 10
_MAX_MESSAGE = 300


class _Failure(Exception):
    def __init__(self, code: str, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse repeats unrecognized arguments, which could be a pasted database URL.
        if message.startswith("unrecognized arguments"):
            message = "unrecognized arguments (not repeated: they may contain secrets)"
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="Ingest one commit of a public GitHub repository as a snapshot in "
        "PostgreSQL and print the result as JSON.",
        epilog=f"The database is taken from {DATABASE_URL_VARIABLE} (a database migrated with "
        "db/migrations); it is never accepted as an argument or printed.",
    )
    parser.add_argument("url", metavar="URL", help="https://github.com/OWNER/NAME")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ref", help="branch, tag, refs/... name or HEAD; resolved to a commit")
    target.add_argument("--commit-sha", metavar="SHA", help="full 40- or 64-character commit SHA")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)  # usage errors exit 2
    try:
        result = _run(args.url, commit_sha=args.commit_sha, ref=args.ref)
    except _Failure as failure:
        print(f"{PROG}: {failure.code}: {failure.message}", file=sys.stderr)
        return failure.exit_code
    sys.stdout.write(json.dumps(result_json(result)) + "\n")
    note = skipped_note(result)
    if note:
        print(f"{PROG}: note: {note}", file=sys.stderr)
    return 0


def result_json(result: IngestResult) -> dict[str, Any]:
    return {
        "snapshot_id": result.snapshot_id,
        "repository": result.repository.model_dump(mode="json"),
        "commit_sha": result.commit_sha,
        "extractor_version": result.extractor_version,
        "config_hash": result.config_hash,
        "already_existed": result.already_existed,
        "skipped": None
        if result.skipped is None
        else [{"path": entry.path, "reason": entry.reason} for entry in result.skipped],
    }


def skipped_note(result: IngestResult) -> str | None:
    if not result.skipped:
        return None
    listed = ", ".join(f"{e.reason} {e.path!r}" for e in result.skipped[:_NOTED_ENTRIES])
    more = len(result.skipped) - _NOTED_ENTRIES
    return f"{len(result.skipped)} tree entries are not represented in the snapshot: {listed}" + (
        f", and {more} more" if more > 0 else ""
    )


def _connect(database_url: str) -> Connection:
    return psycopg.connect(database_url, autocommit=True)


def _run(url: str, *, commit_sha: str | None, ref: str | None) -> IngestResult:
    try:
        ingestion_target(url, commit_sha=commit_sha, ref=ref)  # before touching the database
    except IngestionInputError as exc:
        raise _Failure("invalid_input", 2, str(exc)) from None
    database_url = os.environ.get(DATABASE_URL_VARIABLE)
    if not database_url:
        raise _Failure("configuration", 2, f"{DATABASE_URL_VARIABLE} is not set")
    try:
        conn = _connect(database_url)
    except psycopg.Error:  # its message can repeat the connection string
        raise _Failure(
            "database_unavailable", 6, f"cannot connect to the database in {DATABASE_URL_VARIABLE}"
        ) from None
    with conn:
        try:
            return ingest(conn, url, commit_sha=commit_sha, ref=ref)
        except Exception as exc:
            raise _failure(exc) from None


def _failure(exc: Exception) -> _Failure:
    """The reported failure for an exception from ``ingest``. Messages of the ingestion,
    retrieval and persistence errors are written to be shown; others are not shown."""
    if isinstance(exc, IngestionInputError):
        return _Failure("invalid_input", 2, str(exc))
    if isinstance(exc, RetrievalTimeoutError):
        return _Failure("timeout", 4, str(exc))
    if isinstance(exc, RetrievalLimitError):
        return _Failure("limit_exceeded", 4, f"{exc} ({exc.limit})")
    if isinstance(exc, RetrievalError):
        return _Failure("retrieval_failed", 3, str(exc))
    if isinstance(exc, AnalyzerContractError):
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return _Failure("analysis_failed", 5, first_line[:_MAX_MESSAGE])
    if isinstance(exc, ValidationError):
        return _Failure("analysis_failed", 5, "the analyzer rejected its input")
    if isinstance(exc, PersistenceError):
        return _Failure("database_error", 6, str(exc)[:_MAX_MESSAGE])
    if isinstance(exc, psycopg.Error):
        sqlstate = getattr(exc, "sqlstate", None)
        return _Failure("database_error", 6, f"{type(exc).__name__}"
                        + (f" (SQLSTATE {sqlstate})" if sqlstate else ""))  # fmt: skip
    return _Failure("unexpected_error", 1, f"{type(exc).__name__}; details are not printed")


if __name__ == "__main__":
    raise SystemExit(main())
