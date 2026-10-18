"""Retrieve one commit of a public GitHub repository into a temporary directory.

``retrieve(target)`` is a context manager: inside it, ``RetrievedTree.root`` holds the
commit's regular files exactly as git stores them; on every exit (success, error,
timeout) the whole temporary directory is removed.

Steps, each bounded by ``RetrievalLimits``:

1. ``git init --bare`` a private repository in a fresh temporary directory.
2. A ref is resolved with ``git ls-remote`` against exact names only
   (``refs/heads/<ref>``, ``refs/tags/<ref>``, a full ``refs/...`` name or ``HEAD``);
   a name that is both a branch and a tag is rejected as ambiguous.
3. ``git fetch --depth=1 --no-tags --filter=blob:limit=<max_file_bytes + 1>`` of that
   one commit, with ``transfer.fsckObjects``. Commits and trees are always fetched;
   blobs over ``max_file_bytes`` are left out when the server supports filtering
   (GitHub does) and fetched otherwise. The size of the git directory is watched while
   it runs (``max_git_bytes``).
4. ``git ls-tree -r -l -z`` lists the tree. Every limit, path rule and case collision
   is checked over the whole listing before any file is written; a regular file whose
   blob was left out by the filter fails ``max_file_bytes`` like one that was fetched.
5. Regular files (modes ``100644``/``100755``) are streamed from ``git cat-file --batch``;
   each blob's length and object id are verified while it is written. Symlinks
   (``120000``) and submodules (``160000``) are skipped and reported, never followed.

Git runs from an absolute executable found only in absolute ``PATH`` directories (never
the current directory, never a Windows batch file), with an argument list (no shell), a minimal
environment, no system or global configuration, no credential helpers or prompts, only
the one transport protocol allowed, no HTTP redirects, and no hooks. Each process is
killed with its descendants at its timeout. The remote is always
``https://github.com/<owner>/<name>.git``, built from the validated repository; the
user's URL never reaches git. Errors never include local paths or git's own output.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple

from xraymyrepo.cim import RepositoryRef

from .github import GITHUB_HOST, IngestionInputError, IngestionTarget, validate_commit_sha

MiB = 1024 * 1024

MAX_PATH_BYTES = 1024
"""Longest repository-relative path accepted, in UTF-8 bytes."""
MAX_COMPONENT_BYTES = 255
_LISTING_RECORD_BYTES = 160
"""Upper bound of one ``ls-tree -l -z`` record without its path."""
_MAX_LS_REMOTE_BYTES = 1 * MiB
_MAX_SMALL_OUTPUT = 64 * 1024
_STDERR_TAIL = 8 * 1024
_CHUNK = 64 * 1024
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class RetrievalLimits:
    """Resource limits of one retrieval. Exceeding any of them fails the retrieval."""

    max_files: int = 20_000
    """Tree entries of the commit, counting regular files, symlinks and submodules."""
    max_file_bytes: int = 1 * MiB
    """Size of any one regular file. A larger file fails the retrieval rather than being
    skipped, so a snapshot never silently lacks a source file. Larger blobs are not
    downloaded when the server supports partial-clone filtering."""
    max_total_bytes: int = 200 * MiB
    """Sum of the sizes of all regular files written."""
    max_git_bytes: int = 300 * MiB
    """Bytes of all files in the temporary git directory (packs, indexes, temporary
    packs being received). Sampled while ``git fetch`` runs, and checked once more
    after it exits."""
    command_timeout: float = 300.0
    """Seconds any one git command may run."""
    deadline: float = 600.0
    """Seconds the whole retrieval may take, from start to the materialized tree. Callers
    making several calls for one ingestion share one deadline (``ends_at``)."""

    def __post_init__(self) -> None:
        for name in ("max_files", "max_file_bytes", "max_total_bytes", "max_git_bytes",
                     "command_timeout", "deadline"):  # fmt: skip
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")


class RetrievalError(Exception):
    """A repository commit could not be retrieved."""


class GitUnavailableError(RetrievalError):
    """No usable git executable."""


class RemoteUnavailableError(RetrievalError):
    """The repository does not exist, is private, or cannot be reached."""


class RefResolutionError(RetrievalError):
    """The ref does not exist in the repository, or names both a branch and a tag."""


class CommitNotFoundError(RetrievalError):
    """The commit does not exist in the repository, or the object is not a commit."""


class RetrievalLimitError(RetrievalError):
    """A ``RetrievalLimits`` limit was exceeded; ``limit`` names it."""

    def __init__(self, limit: str, detail: str) -> None:
        super().__init__(detail)
        self.limit = limit


class UnsafeTreeError(RetrievalError):
    """The tree has a path or entry that cannot be written safely and portably."""


class RetrievalTimeoutError(RetrievalError):
    """A git command or the whole retrieval ran out of time."""


@dataclass(frozen=True)
class SkippedEntry:
    path: str
    reason: Literal["symlink", "submodule"]


@dataclass(frozen=True)
class RetrievedTree:
    repository: RepositoryRef
    commit_sha: str
    root: Path
    """Directory holding the commit's regular files; removed when ``retrieve`` exits."""
    file_count: int
    total_bytes: int
    skipped: tuple[SkippedEntry, ...]
    """Tree entries not written, in tree order."""


class TreeEntry(NamedTuple):
    path: str
    mode: str
    kind: str
    oid: str
    size: int | None
    """None for a submodule, or for a blob that was not fetched (``ls-tree -l`` cannot
    read its size)."""


class TreePlan(NamedTuple):
    files: tuple[TreeEntry, ...]
    skipped: tuple[SkippedEntry, ...]
    total_bytes: int


# ---------------------------------------------------------------------------
# Entry point


@contextmanager
def retrieve(
    target: IngestionTarget,
    *,
    limits: RetrievalLimits | None = None,
    temp_dir: Path | None = None,
    ends_at: float | None = None,
) -> Iterator[RetrievedTree]:
    """Retrieve ``target`` and yield the materialized tree; remove everything on exit.

    ``temp_dir`` is where the temporary directory is created (default: the system's).
    ``ends_at`` is the ``time.monotonic()`` time by which retrieval must finish (default:
    ``limits.deadline`` from now); pass ``deadline(limits)`` taken once to share one
    deadline across ``resolve_commit`` and ``retrieve``.
    """
    with _session(target, limits, temp_dir, ends_at) as session:
        yield session.retrieve(target)


def resolve_commit(
    target: IngestionTarget,
    *,
    limits: RetrievalLimits | None = None,
    temp_dir: Path | None = None,
    ends_at: float | None = None,
) -> str:
    """The full commit SHA ``target`` names, without fetching anything: its commit SHA,
    or its ref resolved with ``git ls-remote`` exactly as ``retrieve`` resolves it.
    ``ends_at`` as for ``retrieve``."""
    if target.commit_sha is not None:
        return target.commit_sha
    assert target.ref is not None
    with _session(target, limits, temp_dir, ends_at) as session:
        session.init("sha1")
        return session.resolve(target.ref)


def deadline(limits: RetrievalLimits | None = None) -> float:
    """The ``ends_at`` of a retrieval starting now: ``limits.deadline`` seconds ahead."""
    return time.monotonic() + (limits or RetrievalLimits()).deadline


@contextmanager
def _session(
    target: IngestionTarget,
    limits: RetrievalLimits | None,
    temp_dir: Path | None,
    ends_at: float | None,
) -> Iterator[_Session]:
    """A session in a fresh temporary directory, removed on every exit."""
    limits = limits or RetrievalLimits()
    end = deadline(limits) if ends_at is None else ends_at
    if time.monotonic() >= end:  # before creating anything
        raise RetrievalTimeoutError("the retrieval ran out of time")
    remote = _remote_for(target.repository)
    command = _git_command()
    work = Path(tempfile.mkdtemp(prefix="xray-ingest-", dir=temp_dir))
    try:
        yield _Session(command, remote, work, limits, end)
    finally:
        _remove_tree(work)


class _Remote(NamedTuple):
    url: str
    protocol: str


def _remote_for(repository: RepositoryRef) -> _Remote:
    """The only remote retrieval talks to. (Tests replace this function to fetch from
    a local repository; nothing else selects a remote.)"""
    if repository.host != GITHUB_HOST:
        raise RemoteUnavailableError(f"only {GITHUB_HOST} repositories can be retrieved")
    return _Remote(f"https://{GITHUB_HOST}/{repository.owner}/{repository.name}.git", "https")


_GIT_EXECUTABLE = "git.exe" if sys.platform == "win32" else "git"


def _git_command(environ: Mapping[str, str] = os.environ) -> tuple[str, ...]:
    """The first ``git`` (``git.exe`` on Windows) in an absolute ``PATH`` directory.

    Not ``shutil.which``: on Windows it searches the current directory first (unless
    ``NoDefaultCurrentDirectoryInExePath`` is set) and accepts ``git.bat``/``git.cmd``/
    ``git.com`` through ``PATHEXT``; batch files run through ``cmd.exe``, which re-parses
    the arguments. Empty and relative ``PATH`` entries are skipped everywhere, since they
    resolve against the current directory.
    """
    for directory in _absolute_path_entries(environ.get("PATH", "")):
        candidate = os.path.join(directory, _GIT_EXECUTABLE)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return (os.path.normpath(candidate),)
    raise GitUnavailableError("git is not installed or not on PATH")


def _absolute_path_entries(path: str) -> list[str]:
    """``PATH`` entries naming an absolute directory; on Windows one with a drive or UNC
    share (``\\bin`` and ``C:bin`` depend on the current drive or directory)."""
    entries = []
    for entry in path.split(os.pathsep):
        if sys.platform == "win32":
            entry = entry.strip('"')
            if not os.path.splitdrive(entry)[0]:
                continue
        if entry and os.path.isabs(entry):
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# One retrieval


_FETCH_NOT_FOUND = re.compile(rb"not our ref|couldn't find remote ref|no such remote ref", re.I)
_FETCH_FSCK = re.compile(rb"fsck error", re.I)


class _GitFailed(Exception):
    """A git command exited non-zero; ``stderr`` is used for classification only."""

    def __init__(self, stderr: bytes) -> None:
        super().__init__("git failed")
        self.stderr = stderr


class _Session:
    def __init__(
        self,
        command: tuple[str, ...],
        remote: _Remote,
        work: Path,
        limits: RetrievalLimits,
        end: float,
    ) -> None:
        self.limits = limits
        self.remote = remote
        self.end = end
        """``time.monotonic()`` time by which every command must have finished."""
        self.work = work
        self.git_dir = work / "git"
        self.tree = work / "tree"
        home, hooks = work / "home", work / "hooks"
        for directory in (home, hooks, self.tree):
            directory.mkdir()
        empty_config = home / "empty.gitconfig"
        empty_config.write_bytes(b"")
        self.env = _environment(home, empty_config, remote.protocol)
        self.base = (*command, *_config(hooks, remote.protocol))

    def init(self, object_format: str) -> None:
        self._run(("init", "--bare", "--quiet", "--template=", f"--object-format={object_format}",
                   str(self.git_dir)), repo=False)  # fmt: skip

    def retrieve(self, target: IngestionTarget) -> RetrievedTree:
        self.init("sha256" if target.commit_sha and len(target.commit_sha) == 64 else "sha1")
        if target.commit_sha is not None:
            sha = target.commit_sha
        else:
            assert target.ref is not None
            sha = self.resolve(target.ref)
        self._fetch(sha)
        plan = plan_tree(self._list_tree(sha), self.limits)
        self._materialize(plan.files, "sha256" if len(sha) == 64 else "sha1")
        return RetrievedTree(target.repository, sha, self.tree, len(plan.files),
                             plan.total_bytes, plan.skipped)  # fmt: skip

    def resolve(self, ref: str) -> str:
        try:
            out = self._run(
                # The second pattern lists an annotated tag's peeled commit (``<tag>^{}``).
                ("ls-remote", self.remote.url, ref, f"{ref}^{{}}"),
                max_output=_MAX_LS_REMOTE_BYTES,
                overflow=RetrievalLimitError("ls_remote_output", "the ref listing is too large"),
            )
        except _GitFailed:
            raise _unavailable() from None
        refs: dict[str, str] = {}
        for line in out.splitlines():
            raw_oid, tab, raw_name = line.partition(b"\t")
            if not tab:
                raise RetrievalError("unexpected output from git ls-remote")
            refs[raw_name.decode("utf-8", "replace")] = raw_oid.decode("ascii", "replace")
        if ref == "HEAD" or ref.startswith("refs/"):
            candidates = [ref]
        else:
            candidates = [f"refs/heads/{ref}", f"refs/tags/{ref}"]
        found = [name for name in candidates if name in refs]
        if not found:
            raise RefResolutionError(f"ref {ref!r} does not exist in the repository")
        if len(found) > 1:
            raise RefResolutionError(
                f"ref {ref!r} is both a branch and a tag; pass refs/heads/{ref} or refs/tags/{ref}"
            )
        oid = refs.get(f"{found[0]}^{{}}", refs[found[0]])  # an annotated tag's commit
        try:
            return validate_commit_sha(oid)
        except IngestionInputError:
            raise RetrievalError("git ls-remote returned an invalid object id") from None

    def _fetch(self, sha: str) -> None:
        limit = self.limits.max_git_bytes
        # --filter needs a promisor remote. It is configured for this command only, so no
        # later command can lazily fetch the blobs it leaves out.
        promisor = ("-c", f"remote.origin.url={self.remote.url}",
                    "-c", "remote.origin.promisor=true",
                    "-c", "extensions.partialClone=origin")  # fmt: skip
        try:
            self._run(
                (*promisor, "fetch", "--quiet", "--depth=1", "--no-tags",
                 "--no-recurse-submodules", "--no-write-fetch-head",
                 f"--filter=blob:limit={self.limits.max_file_bytes + 1}", "origin", sha),
                watch=(self.git_dir, limit),
            )  # fmt: skip
        except _GitFailed as exc:
            if _FETCH_NOT_FOUND.search(exc.stderr):
                raise CommitNotFoundError(
                    f"commit {sha} does not exist in the repository"
                ) from None
            if _FETCH_FSCK.search(exc.stderr):
                raise UnsafeTreeError(
                    "git rejected malformed objects in the commit (transfer.fsckObjects)"
                ) from None
            raise _unavailable() from None
        if _directory_size(self.git_dir) > limit:
            raise _storage_exceeded(limit)
        try:
            kind = self._run(("cat-file", "-t", sha)).strip()
        except _GitFailed:
            kind = b""
        if kind != b"commit":
            raise CommitNotFoundError(f"{sha} is not a commit")

    def _list_tree(self, sha: str) -> list[TreeEntry]:
        limit = self.limits.max_files
        out = self._run(
            ("ls-tree", "-r", "-l", "-z", sha),
            max_output=limit * (MAX_PATH_BYTES + _LISTING_RECORD_BYTES),
            overflow=RetrievalLimitError(
                "max_files", f"the tree has more than {limit} entries or overlong paths"
            ),
        )
        records = out.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        if len(records) > limit:
            raise RetrievalLimitError(
                "max_files", f"the tree has {len(records)} entries; the limit is {limit}"
            )
        return [_parse_listing(record) for record in records]

    def _materialize(self, files: tuple[TreeEntry, ...], hash_name: str) -> None:
        if not files:
            return
        root = self.tree.resolve()
        written = 0
        with self._process(("cat-file", "--batch"), stdin=True) as process:
            assert process.stdin is not None and process.stdout is not None
            try:
                for entry in files:
                    target = _inside(root, entry.path)
                    process.stdin.write(entry.oid.encode("ascii") + b"\n")
                    process.stdin.flush()
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        out = open(target, "xb")  # noqa: SIM115 - closed below
                    except OSError:
                        raise RetrievalError(f"cannot write {entry.path!r}") from None
                    with out:
                        written += copy_blob(
                            process.stdout, entry, out, hash_name, self.limits.max_file_bytes
                        )
                    if written > self.limits.max_total_bytes:
                        raise RetrievalLimitError(
                            "max_total_bytes",
                            f"files exceed {self.limits.max_total_bytes} bytes in total",
                        )
                process.stdin.close()
            except (OSError, RetrievalError) as exc:
                process.kill("failed")
                process.raise_if_killed()
                if isinstance(exc, RetrievalError):
                    raise
                raise RetrievalError("git cat-file stopped unexpectedly") from None
            if process.finish() != 0:
                raise RetrievalError("git cat-file failed")

    # -- running git ---------------------------------------------------------

    def _process(
        self, args: Iterable[str], *, repo: bool = True, stdin: bool = False,
        watch: tuple[Path, int] | None = None,
    ) -> _Process:  # fmt: skip
        argv = [*self.base, *(("--git-dir", str(self.git_dir)) if repo else ()), *args]
        end = min(time.monotonic() + self.limits.command_timeout, self.end)
        return _Process(argv, self.env, self.work, end, stdin=stdin, watch=watch)

    def _run(
        self,
        args: Iterable[str],
        *,
        repo: bool = True,
        max_output: int = _MAX_SMALL_OUTPUT,
        overflow: RetrievalError | None = None,
        watch: tuple[Path, int] | None = None,
    ) -> bytes:
        """Run git to completion and return its stdout; ``_GitFailed`` if it fails."""
        with self._process(args, repo=repo, watch=watch) as process:
            assert process.stdout is not None
            out = process.stdout.read(max_output + 1)
            if len(out) > max_output:
                process.kill("overflow")
            code = process.finish()
        process.raise_if_killed(overflow)
        if code != 0:
            raise _GitFailed(process.stderr_tail())
        return out


def _unavailable() -> RemoteUnavailableError:
    return RemoteUnavailableError(
        "the repository could not be fetched: it may not exist, be private, or be unreachable"
    )


def _storage_exceeded(limit: int) -> RetrievalLimitError:
    return RetrievalLimitError("max_git_bytes", f"fetched git data exceeds {limit} bytes")


def _parse_listing(record: bytes) -> TreeEntry:
    meta, tab, raw_path = record.partition(b"\t")
    fields = meta.split()
    if not tab or len(fields) != 4:
        raise RetrievalError("unexpected output from git ls-tree")
    mode, kind, oid, size = (f.decode("ascii", "replace") for f in fields)
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsafeTreeError(f"path is not valid UTF-8: {raw_path!r}") from None
    return TreeEntry(path, mode, kind, oid, int(size) if size.isdigit() else None)


# ---------------------------------------------------------------------------
# Tree checks (pure)


_REGULAR_MODES = frozenset({"100644", "100755"})
_WINDOWS_INVALID = frozenset('<>:"|?*\\')
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{device}{n}" for device in ("com", "lpt") for n in (*"0123456789", "¹", "²", "³")}
)
_GIT_SHORT_NAME = re.compile(r"^git~\d+$")
_HFS_IGNORABLE = dict.fromkeys(
    [0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)
"""Code points HFS+ ignores in names, so ``.g\\u200cit`` is ``.git`` on macOS."""


def plan_tree(entries: Iterable[TreeEntry], limits: RetrievalLimits) -> TreePlan:
    """Check a whole tree listing before anything is written: every path, entry mode,
    the size limits and case-insensitive collisions. Raises on the first problem, in
    tree order, so the outcome is deterministic."""
    files: list[TreeEntry] = []
    skipped: list[SkippedEntry] = []
    total = 0
    for entry in entries:
        check_path(entry.path)
        if entry.mode in _REGULAR_MODES and entry.kind == "blob":
            if entry.size is None:  # not fetched: the filter left out blobs over the limit
                raise RetrievalLimitError(
                    "max_file_bytes",
                    f"{entry.path!r} is larger than the limit of {limits.max_file_bytes} bytes",
                )
            if entry.size > limits.max_file_bytes:
                raise RetrievalLimitError(
                    "max_file_bytes",
                    f"{entry.path!r} is {entry.size} bytes; the limit is {limits.max_file_bytes}",
                )
            total += entry.size
            files.append(entry)
        elif entry.mode == "120000" and entry.kind == "blob":
            skipped.append(SkippedEntry(entry.path, "symlink"))
        elif entry.mode == "160000" and entry.kind == "commit":
            skipped.append(SkippedEntry(entry.path, "submodule"))
        else:
            raise UnsafeTreeError(
                f"unsupported tree entry {entry.mode} {entry.kind}: {entry.path!r}"
            )
    if total > limits.max_total_bytes:
        raise RetrievalLimitError(
            "max_total_bytes", f"files total {total} bytes; the limit is {limits.max_total_bytes}"
        )
    check_collisions(f.path for f in files)
    return TreePlan(tuple(files), tuple(skipped), total)


def check_path(path: str) -> None:
    """Reject a repository path that is unsafe or not portable to Windows and macOS:
    absolute, empty, ``.``/``..`` or empty components, NUL and control characters,
    backslashes, drive or stream syntax (``:``), other Windows-invalid characters,
    trailing dots or spaces, reserved device names, any spelling of ``.git``
    (including NTFS short names and HFS+ ignorable code points) and overlong paths."""
    if not path:
        raise UnsafeTreeError("empty path")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise UnsafeTreeError(f"path is longer than {MAX_PATH_BYTES} bytes: {path[:80]!r}...")
    if path.startswith("/"):
        raise UnsafeTreeError(f"absolute path: {path!r}")
    for char in path:
        if ord(char) < 0x20 or ord(char) == 0x7F or char in _WINDOWS_INVALID:
            raise UnsafeTreeError(f"path has a character that is not portable: {path!r}")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise UnsafeTreeError(f"path has an empty, '.' or '..' component: {path!r}")
        if len(part.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise UnsafeTreeError(
                f"path component longer than {MAX_COMPONENT_BYTES} bytes: {path!r}"
            )
        if part[-1] in ". ":
            raise UnsafeTreeError(f"path component ends with a dot or space: {path!r}")
        folded = part.translate(_HFS_IGNORABLE).casefold()
        if folded == ".git" or _GIT_SHORT_NAME.fullmatch(folded):
            raise UnsafeTreeError(f"path has a .git component: {path!r}")
        if folded.split(".", 1)[0].rstrip(" ") in _RESERVED_NAMES:
            raise UnsafeTreeError(f"path has a reserved Windows device name: {path!r}")


def check_collisions(paths: Iterable[str]) -> None:
    """Reject paths, or their directories, that differ only in case or Unicode
    normalization: they would overwrite each other on case-insensitive file systems."""
    seen: dict[str, tuple[str, bool]] = {}
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            name = "/".join(parts[:depth])
            entry = (name, depth == len(parts))
            key = unicodedata.normalize("NFC", name).casefold()
            previous = seen.setdefault(key, entry)
            if previous is not entry and (previous != entry or entry[1]):
                raise UnsafeTreeError(
                    f"paths collide on case-insensitive file systems: {previous[0]!r} and {name!r}"
                )


def copy_blob(
    stream: IO[bytes], entry: TreeEntry, out: IO[bytes], hash_name: str, max_bytes: int
) -> int:
    """Copy one ``git cat-file --batch`` response for ``entry`` into ``out`` and return
    its length. The header must name the expected blob with the listed size, at most
    ``max_bytes``; the content is streamed, counted and hashed, and its git object id
    must equal ``entry.oid``."""
    header = stream.readline(256).split()
    if len(header) != 3 or header[0].decode("ascii", "replace") != entry.oid:
        raise RetrievalError(f"git returned an unexpected object for {entry.path!r}")
    if header[1] != b"blob" or not header[2].isdigit():
        raise RetrievalError(f"git returned an unexpected object for {entry.path!r}")
    size = int(header[2])
    if size > max_bytes:
        raise RetrievalLimitError(
            "max_file_bytes", f"{entry.path!r} is {size} bytes; the limit is {max_bytes}"
        )
    if size != entry.size:
        raise RetrievalError(f"the size of {entry.path!r} does not match the tree listing")
    digest = hashlib.new(hash_name, b"blob %d\x00" % size)
    remaining = size
    while remaining:
        chunk = stream.read(min(_CHUNK, remaining))
        if not chunk:
            raise RetrievalError(f"the content of {entry.path!r} is truncated")
        digest.update(chunk)
        out.write(chunk)
        remaining -= len(chunk)
    if stream.read(1) != b"\n":
        raise RetrievalError(f"git returned malformed output for {entry.path!r}")
    if digest.hexdigest() != entry.oid:
        raise RetrievalError(f"the content of {entry.path!r} does not match its object id")
    return size


def _inside(root: Path, path: str) -> Path:
    target = root.joinpath(*path.split("/"))
    if not target.resolve().is_relative_to(root):
        raise UnsafeTreeError(f"path escapes the repository root: {path!r}")
    return target


# ---------------------------------------------------------------------------
# Processes, environment, cleanup


_PASSTHROUGH = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
)  # fmt: skip
"""Inherited variables: what git needs to start and reach the network (proxies, CA
certificates). Everything else, notably every ``GIT_*`` variable, is dropped."""


def _environment(
    home: Path, empty_config: Path, protocol: str, environ: Mapping[str, str] = os.environ
) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in _PASSTHROUGH:
        value = environ.get(name)
        if value is not None and name.upper() not in {k.upper() for k in env}:
            env[name] = value
    if "PATH" in env:  # git looks up its helpers on PATH too
        env["PATH"] = os.pathsep.join(_absolute_path_entries(env["PATH"]))
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=str(empty_config),
        GIT_CONFIG_GLOBAL=str(empty_config),
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        GIT_ALLOW_PROTOCOL=protocol,
        GIT_PROTOCOL_FROM_USER="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_OPTIONAL_LOCKS="0",
        LC_ALL="C",
        LANG="C",
    )
    return env


def _config(hooks: Path, protocol: str) -> tuple[str, ...]:
    settings = (
        "protocol.allow=never",
        f"protocol.{protocol}.allow=always",
        "credential.helper=",
        "core.askPass=",
        f"core.hooksPath={hooks}",
        "core.fsmonitor=false",
        "http.followRedirects=false",
        "http.lowSpeedLimit=1024",
        "http.lowSpeedTime=60",
        "transfer.fsckObjects=true",
        "fetch.recurseSubmodules=false",
        "submodule.recurse=false",
        "fetch.writeCommitGraph=false",
        "gc.auto=0",
        "maintenance.auto=false",
    )
    return tuple(arg for setting in settings for arg in ("-c", setting))


class _Process:
    """A git process that is killed, with its descendants, at ``end`` or when a watched
    directory grows past its cap. Its stderr is drained in the background, keeping the
    tail only."""

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        cwd: Path,
        end: float,
        *,
        stdin: bool,
        watch: tuple[Path, int] | None,
    ) -> None:
        kwargs: dict[str, Any]
        if sys.platform == "win32":
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                      | subprocess.CREATE_NO_WINDOW}  # fmt: skip
        else:
            kwargs = {"start_new_session": True}
        self.reason: str | None = None
        self._end = end
        self._watch = watch
        self._stderr = bytearray()
        self._done = threading.Event()
        self._lock = threading.Lock()
        if time.monotonic() >= end:
            raise RetrievalTimeoutError("the retrieval ran out of time")
        self._proc: subprocess.Popen[bytes] = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            **kwargs,
        )
        self.stdin = self._proc.stdin
        self.stdout = self._proc.stdout
        self._threads = [threading.Thread(target=self._drain, daemon=True),
                         threading.Thread(target=self._watchdog, daemon=True)]  # fmt: skip
        for thread in self._threads:
            thread.start()

    def __enter__(self) -> _Process:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc.poll() is None:
            self.kill("abandoned")
        self.finish()

    def _drain(self) -> None:
        stream = self._proc.stderr
        assert stream is not None
        while chunk := stream.read1(_CHUNK):  # type: ignore[attr-defined]
            self._stderr += chunk
            del self._stderr[:-_STDERR_TAIL]

    def _watchdog(self) -> None:
        while not self._done.wait(_POLL_SECONDS):
            if self._proc.poll() is not None:
                return
            if time.monotonic() >= self._end:
                self.kill("timeout")
            elif self._watch is not None and _directory_size(self._watch[0]) > self._watch[1]:
                self.kill("storage")

    def kill(self, reason: str) -> None:
        with self._lock:
            if self.reason is None:
                self.reason = reason
        if self._proc.poll() is None:
            _kill_tree(self._proc)

    def finish(self) -> int:
        """Wait for the process, close its pipes and stop the helper threads."""
        try:
            code = self._proc.wait(timeout=max(1.0, self._end - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.kill("timeout")
            code = self._proc.wait()
        self._done.set()
        for stream in (self._proc.stdin, self._proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        for thread in self._threads:
            thread.join()
        assert self._proc.stderr is not None
        self._proc.stderr.close()
        return code

    def raise_if_killed(self, overflow: RetrievalError | None = None) -> None:
        if self.reason == "timeout":
            raise RetrievalTimeoutError("a git command ran out of time")
        if self.reason == "storage":
            assert self._watch is not None
            raise _storage_exceeded(self._watch[1])
        if self.reason == "overflow":
            raise overflow or RetrievalError("git produced too much output")

    def stderr_tail(self) -> bytes:
        return bytes(self._stderr)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and its descendants (git runs helpers such as ``git-remote-https``
    and ``index-pack``)."""
    if sys.platform == "win32":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        taskkill = os.path.join(system_root, "System32", "taskkill.exe")
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)  # fmt: skip
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _directory_size(root: Path) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            with contextlib.suppress(OSError):  # removed while git was writing
                total += os.lstat(os.path.join(directory, name)).st_size
    return total


def _remove_tree(path: Path) -> None:
    """Remove the temporary directory, including git's read-only pack files on Windows."""

    def make_writable(function: Any, name: str, _exc: BaseException) -> None:
        os.chmod(name, stat.S_IWRITE)
        function(name)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable)
