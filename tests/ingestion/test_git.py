"""Git retrieval against local repositories (no network unless XRAY_TEST_GITHUB is set).

Origin repositories are built with git plumbing (``hash-object``, ``mktree``,
``commit-tree``), so trees can hold entries no checkout could produce: symlinks,
submodules, case collisions and Windows-invalid names.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.ingestion import (
    CommitNotFoundError,
    GitUnavailableError,
    IngestionTarget,
    RefResolutionError,
    RemoteUnavailableError,
    RetrievalError,
    RetrievalLimitError,
    RetrievalLimits,
    RetrievalTimeoutError,
    SkippedEntry,
    UnsafeTreeError,
    ingestion_target,
    resolve_commit,
    retrieve,
)
from xraymyrepo.ingestion import git as retrieval
from xraymyrepo.ingestion.git import TreeEntry, check_collisions, check_path, copy_blob, plan_tree

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

URL = "https://github.com/acme/app"
GITLINK = "0123456789abcdef0123456789abcdef01234567"
_DATE = {"GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
_PROXIES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

Spec = Mapping[str, "bytes | tuple[str, bytes | str]"]
"""Repository path -> content (mode 100644), or (mode, content); a 160000 entry's
content is the submodule commit id."""


def noise(size: int) -> bytes:
    """Deterministic, incompressible bytes."""
    out = bytearray()
    block = b"seed"
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


class Origin:
    """A bare repository standing in for GitHub, built without a working tree."""

    def __init__(self, path: Path) -> None:
        self.path = path
        subprocess.run([str(GIT), "init", "--quiet", "--bare", "--initial-branch=main", str(path)],
                       check=True)  # fmt: skip
        self.filtering(True)

    def filtering(self, allowed: bool) -> None:
        """Whether the server honours ``--filter`` (GitHub does)."""
        self.git("config", "uploadpack.allowFilter", "true" if allowed else "false")

    @property
    def url(self) -> str:
        return self.path.as_uri()

    def git(self, *args: str, data: bytes | None = None) -> str:
        result = subprocess.run(
            [str(GIT), "--git-dir", str(self.path), "-c", "user.name=t", "-c", "user.email=t@t",
             *args],
            input=data, capture_output=True, check=True, env={**os.environ, **_DATE},
        )  # fmt: skip
        return result.stdout.decode().strip()

    def tree(self, spec: Spec) -> str:
        levels: dict[str, Any] = {}
        for path, value in spec.items():
            *dirs, name = path.split("/")
            level = levels
            for directory in dirs:
                level = level.setdefault(directory, {})
            level[name] = value if isinstance(value, tuple) else ("100644", value)
        return self._tree(levels)

    def _tree(self, level: dict[str, Any]) -> str:
        lines = []
        for name, value in level.items():
            if isinstance(value, dict):
                lines.append(f"040000 tree {self._tree(value)}\t{name}")
            elif value[0] == "160000":
                lines.append(f"160000 commit {value[1]}\t{name}")
            else:
                oid = self.git("hash-object", "-w", "--stdin", data=value[1])
                lines.append(f"{value[0]} blob {oid}\t{name}")
        return self.git("mktree", "--missing", data="".join(f"{x}\n" for x in lines).encode())

    def commit(self, spec: Spec, ref: str = "refs/heads/main") -> str:
        parent = self.git("for-each-ref", "--format=%(objectname)", ref)
        sha = self.git(
            "commit-tree", self.tree(spec), "-m", "m", *(["-p", parent] if parent else [])
        )
        self.git("update-ref", ref, sha)
        return sha


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    repo = Origin(tmp_path / "origin.git")
    monkeypatch.setattr(retrieval, "_remote_for", lambda _: retrieval._Remote(repo.url, "file"))
    return repo


@pytest.fixture
def work(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "work"
    path.mkdir()
    yield path
    assert list(path.iterdir()) == [], "the temporary directory was not removed"


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROXIES:
        monkeypatch.delenv(name, raising=False)


def by_sha(sha: str) -> IngestionTarget:
    return ingestion_target(URL, commit_sha=sha)


def by_ref(ref: str) -> IngestionTarget:
    return ingestion_target(URL, ref=ref)


def files_under(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


# ---------------------------------------------------------------------------
# Retrieval


CONTENT: Spec = {
    "README.md": b"hello\n",
    "pkg/__init__.py": b"",
    "pkg/mod.py": b"x = 1\r\ny = 2\n",  # line endings are preserved, never converted
    "bin/run.sh": ("100755", b"#!/bin/sh\necho hi\n"),
    "data/blob.bin": bytes(range(256)) * 40,
    "link": ("120000", b"README.md"),
    "vendor/lib": ("160000", GITLINK),
}
REGULAR = {k: v if isinstance(v, bytes) else v[1] for k, v in CONTENT.items()
           if k not in ("link", "vendor/lib")}  # fmt: skip


def test_retrieves_exact_files_and_skips_links(origin: Origin, work: Path) -> None:
    sha = origin.commit(CONTENT)
    with retrieve(by_sha(sha), temp_dir=work) as tree:
        assert tree.commit_sha == sha
        assert tree.repository.slug == "github.com/acme/app"
        assert tree.root.is_relative_to(work)
        assert files_under(tree.root) == REGULAR
        assert tree.file_count == len(REGULAR)
        assert tree.total_bytes == sum(len(v) for v in REGULAR.values())
        assert tree.skipped == (SkippedEntry("link", "symlink"),
                                SkippedEntry("vendor/lib", "submodule"))  # fmt: skip
        assert not (tree.root / "link").exists() and not (tree.root / "vendor").exists()
        listed = dict(
            line.split(" ", 2)[2].split("\t")[::-1]
            for line in origin.git("ls-tree", "-r", sha).splitlines()
        )
        for path, data in files_under(tree.root).items():
            assert git_blob_id(data) == listed[path]
        root = tree.root
    assert not root.exists()


def test_uppercase_sha_input_is_normalized(origin: Origin, work: Path) -> None:
    sha = origin.commit({"a.py": b"a\n"})
    with retrieve(ingestion_target(URL, commit_sha=sha.upper()), temp_dir=work) as tree:
        assert tree.commit_sha == sha


def test_empty_tree(origin: Origin, work: Path) -> None:
    sha = origin.commit({})
    with retrieve(by_sha(sha), temp_dir=work) as tree:
        assert tree.file_count == 0 and files_under(tree.root) == {}


@pytest.fixture
def refs(origin: Origin) -> dict[str, str]:
    first = origin.commit({"a.py": b"1\n"})
    second = origin.commit({"a.py": b"2\n"})
    origin.git("tag", "v1", first)
    origin.git("tag", "-a", "-m", "annotated", "v2", first)
    return {"first": first, "second": second}


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("main", "second"), ("refs/heads/main", "second"), ("HEAD", "second"),
     ("v1", "first"), ("refs/tags/v1", "first"), ("v2", "first"), ("refs/tags/v2", "first")],
)  # fmt: skip
def test_resolves_refs_to_commits(
    origin: Origin, refs: dict[str, str], work: Path, ref: str, expected: str
) -> None:
    with retrieve(by_ref(ref), temp_dir=work) as tree:
        assert tree.commit_sha == refs[expected]
        assert files_under(tree.root) == {"a.py": b"2\n" if expected == "second" else b"1\n"}


@pytest.mark.parametrize("ref", ["mai", "heads/main", "refs/heads/nope", "nope", "refs/tags/main"])
def test_missing_refs(origin: Origin, refs: dict[str, str], work: Path, ref: str) -> None:
    with (
        pytest.raises(RefResolutionError, match="does not exist"),
        retrieve(by_ref(ref), temp_dir=work),
    ):
        pass


@pytest.mark.parametrize(("ref", "expected"), [("main", "second"), ("v2", "first")])
def test_resolve_commit_matches_retrieve(
    origin: Origin, refs: dict[str, str], work: Path, ref: str, expected: str
) -> None:
    assert resolve_commit(by_ref(ref), temp_dir=work) == refs[expected]


def test_resolve_commit_of_a_sha_needs_no_git(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "_git_command", lambda: pytest.fail("git must not run"))
    assert resolve_commit(by_sha("A" * 40), temp_dir=work) == "a" * 40


def test_resolve_commit_errors(origin: Origin, refs: dict[str, str], work: Path) -> None:
    with pytest.raises(RefResolutionError):
        resolve_commit(by_ref("nope"), temp_dir=work)


def test_ambiguous_ref(origin: Origin, refs: dict[str, str], work: Path) -> None:
    origin.git("tag", "main", refs["first"])
    with (
        pytest.raises(RefResolutionError, match="both a branch and a tag"),
        retrieve(by_ref("main"), temp_dir=work),
    ):
        pass
    with retrieve(by_ref("refs/tags/main"), temp_dir=work) as tree:
        assert tree.commit_sha == refs["first"]


def test_missing_commit(origin: Origin, refs: dict[str, str], work: Path) -> None:
    with pytest.raises(CommitNotFoundError), retrieve(by_sha("1" * 40), temp_dir=work):
        pass


def test_object_that_is_not_a_commit(origin: Origin, work: Path) -> None:
    origin.commit({"a.py": b"a\n"})
    tree_id = origin.git("rev-parse", "main^{tree}")
    with pytest.raises(CommitNotFoundError), retrieve(by_sha(tree_id), temp_dir=work):
        pass


def test_missing_repository_does_not_leak_paths(
    tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = (tmp_path / "missing.git").as_uri()
    monkeypatch.setattr(retrieval, "_remote_for", lambda _: retrieval._Remote(missing, "file"))
    with pytest.raises(RemoteUnavailableError) as raised, retrieve(by_ref("main"), temp_dir=work):
        pass
    assert str(tmp_path) not in str(raised.value) and "missing" not in str(raised.value)


def test_cleanup_when_the_caller_fails(origin: Origin, work: Path) -> None:
    sha = origin.commit({"a.py": b"a\n"})
    with pytest.raises(ZeroDivisionError), retrieve(by_sha(sha), temp_dir=work):
        _ = 1 / 0


def test_inherited_git_environment_is_ignored(
    origin: Origin, work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = origin.commit({"a.py": b"a\n"})
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    with retrieve(by_sha(sha), temp_dir=work) as tree:
        assert files_under(tree.root) == {"a.py": b"a\n"}


def test_environment_is_minimal(tmp_path: Path) -> None:
    path = os.pathsep.join([str(tmp_path), "", ".", "relative/bin"])
    environ = {"PATH": path, "GIT_DIR": "x", "GIT_ASKPASS": "evil", "SSH_ASKPASS": "evil",
               "HOME": "/home/me", "https_proxy": "http://proxy"}  # fmt: skip
    env = retrieval._environment(tmp_path, tmp_path / "c", "https", environ)
    assert env["PATH"] == str(tmp_path)  # relative entries resolve against the cwd
    assert env["https_proxy"] == "http://proxy"
    assert env["HOME"] == str(tmp_path)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ALLOW_PROTOCOL"] == "https"
    assert not {"GIT_DIR", "GIT_ASKPASS", "SSH_ASKPASS"} & set(env)


def test_the_only_production_remote_is_github() -> None:
    repository = ingestion_target("https://github.com/Acme/App.git", ref="main").repository
    assert retrieval._remote_for(repository) == ("https://github.com/acme/app.git", "https")
    other = repository.model_copy(update={"host": "example.com"})
    with pytest.raises(RemoteUnavailableError):
        retrieval._remote_for(other)


# ---------------------------------------------------------------------------
# Non-prompting failures and redirects (a local HTTP server; no internet)


@pytest.fixture
def http_server() -> Iterator[tuple[str, list[str]]]:
    """Answers every request with 401 (asking for credentials) or, for paths under
    /redirect/, with a 302 to /target/; records each request's Authorization header."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(f"{self.path.split('?')[0]} {self.headers.get('Authorization')}")
            if self.path.startswith("/redirect/"):
                self.send_response(302)
                self.send_header("Location", "/target/acme/app.git/info/refs")
            else:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def test_credentials_are_never_prompted_for(
    http_server: tuple[str, list[str]], work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, seen = http_server
    remote = retrieval._Remote(f"{base}/private/app.git", "http")
    monkeypatch.setattr(retrieval, "_remote_for", lambda _: remote)
    started = time.monotonic()
    limits = RetrievalLimits(command_timeout=30)
    with (
        pytest.raises(RemoteUnavailableError),
        retrieve(by_ref("main"), limits=limits, temp_dir=work),
    ):
        pass
    assert time.monotonic() - started < 25
    assert seen and all(entry.endswith(" None") for entry in seen)  # no Authorization header


def test_redirects_are_not_followed(
    http_server: tuple[str, list[str]], work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, seen = http_server
    monkeypatch.setattr(
        retrieval, "_remote_for", lambda _: retrieval._Remote(f"{base}/redirect/app.git", "http")
    )
    with pytest.raises(RemoteUnavailableError), retrieve(by_ref("main"), temp_dir=work):
        pass
    assert seen and not any(entry.startswith("/target/") for entry in seen)


# ---------------------------------------------------------------------------
# Limits


@pytest.mark.parametrize(
    ("spec", "limits", "limit"),
    [
        ({"a": b"1", "b": b"2", "c": b"3"}, RetrievalLimits(max_files=2), "max_files"),
        ({"a": b"1", "l": ("120000", b"a"), "s": ("160000", GITLINK)}, RetrievalLimits(max_files=2),
         "max_files"),
        ({"ok.py": b"x", "big.py": b"x" * 1001}, RetrievalLimits(max_file_bytes=1000),
         "max_file_bytes"),
        ({"a.py": b"x" * 800, "b.py": b"x" * 800}, RetrievalLimits(max_total_bytes=1500),
         "max_total_bytes"),
        ({"noise.bin": noise(256 * 1024)}, RetrievalLimits(max_git_bytes=64 * 1024),
         "max_git_bytes"),
    ],
)  # fmt: skip
def test_limits(
    origin: Origin, work: Path, spec: Spec, limits: RetrievalLimits, limit: str
) -> None:
    sha = origin.commit(spec)
    with (
        pytest.raises(RetrievalLimitError) as raised,
        retrieve(by_sha(sha), limits=limits, temp_dir=work),
    ):
        pass
    assert raised.value.limit == limit


def test_limits_at_their_bounds_pass(origin: Origin, work: Path) -> None:
    sha = origin.commit({"a.py": b"x" * 1000, "b.py": b"x" * 500})
    limits = RetrievalLimits(max_files=2, max_file_bytes=1000, max_total_bytes=1500)
    with retrieve(by_sha(sha), limits=limits, temp_dir=work) as tree:
        assert tree.total_bytes == 1500


def test_a_failed_limit_writes_no_files(
    origin: Origin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an oversized file anywhere in the tree fails the retrieval before any
    file is written, so no partial tree that looks complete can be left behind."""
    spec: dict[str, bytes] = {f"src/m{i:02}.py": b"x = 1\n" for i in range(30)}
    spec["src/zz_big.py"] = b"#" * 2048
    sha = origin.commit(spec)
    kept: list[Path] = []
    remove_tree = retrieval._remove_tree
    monkeypatch.setattr(retrieval, "_remove_tree", kept.append)  # keep it to inspect
    with (
        pytest.raises(RetrievalLimitError, match=r"zz_big\.py"),
        retrieve(by_sha(sha), limits=RetrievalLimits(max_file_bytes=1024), temp_dir=tmp_path),
    ):
        pass
    [work] = kept
    assert list((work / "tree").iterdir()) == []
    remove_tree(work)


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_files"):
        RetrievalLimits(max_files=0)
    with pytest.raises(ValueError, match="deadline"):
        RetrievalLimits(deadline=-1)


# ---------------------------------------------------------------------------
# Unsafe trees


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"a\\b.py": b"x"}, "not portable"),
        ({"src/con.txt": b"x"}, "reserved"),
        ({"AUX": b"x"}, "reserved"),
        ({"a:b": b"x"}, "not portable"),
        ({"trailing.": b"x"}, "dot or space"),
        ({"A.py": b"1", "a.py": b"2"}, "collide"),
        ({"Src/a.py": b"1", "src/b.py": b"2"}, "collide"),
    ],
)
def test_unsafe_trees_write_nothing(origin: Origin, work: Path, spec: Spec, message: str) -> None:
    sha = origin.commit(spec)
    with pytest.raises(UnsafeTreeError, match=message), retrieve(by_sha(sha), temp_dir=work):
        pass


@pytest.mark.parametrize("name", ["..", ".git", ".GIT"])
def test_git_rejects_dot_entries_on_fetch(origin: Origin, work: Path, name: str) -> None:
    """transfer.fsckObjects refuses these before the tree is ever listed."""
    sha = origin.commit({f"{name}/x" if name == ".GIT" else name: b"x"})
    with pytest.raises(UnsafeTreeError, match="fsck"), retrieve(by_sha(sha), temp_dir=work):
        pass


@pytest.mark.parametrize(
    "path",
    ["a.py", "src/pkg/__init__.py", ".github/workflows/ci.yml", "a b/c d.py", "ünïcödé.py",
     "con_fig.py", "console.py", "x.git", "git~x", "a" * 255],
)  # fmt: skip
def test_safe_paths(path: str) -> None:
    check_path(path)


@pytest.mark.parametrize(
    "path",
    ["", "/abs", "a//b", "a/", "./a", "a/./b", "../a", "a/../b", "..", "a\x00b", "a\nb",
     "a\x7fb", "a\\b", "C:/x", "c:", "a:stream", "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b",
     "dir./x", "dir /x", "x.", "x ", ".git", ".git/config", "sub/.Git/x", "GIT~1/x",
     ".g\u200cit/x", "\ufeff.git", "CON", "con.txt", "Com1.py", "lpt9", "nul.tar.gz",
     "conin$", "COM\u00b9", "aux .txt", "a" * 256, "/".join(["d"] * 600)],
)  # fmt: skip
def test_unsafe_paths(path: str) -> None:
    with pytest.raises(UnsafeTreeError):
        check_path(path)


@pytest.mark.parametrize(
    "paths",
    [["README", "readme"], ["a", "A/b"], ["A/b", "a"], ["d/X.py", "D/y.py"],
     ["caf\u00e9.py", "cafe\u0301.py"], ["a.py", "a.py"], ["STRASSE", "straße"]],
)  # fmt: skip
def test_collisions(paths: list[str]) -> None:
    with pytest.raises(UnsafeTreeError, match="collide"):
        check_collisions(paths)


def test_no_collisions() -> None:
    check_collisions(["a/b.py", "a/c.py", "a/d/e.py", "b.py", "B1.py"])


def test_plan_checks_everything_before_writing() -> None:
    entries = [TreeEntry("a.py", "100644", "blob", "0" * 40, 10),
               TreeEntry("b.py", "100755", "blob", "1" * 40, 20),
               TreeEntry("l", "120000", "blob", "2" * 40, 3),
               TreeEntry("s", "160000", "commit", GITLINK, None)]  # fmt: skip
    plan = plan_tree(entries, RetrievalLimits())
    assert [f.path for f in plan.files] == ["a.py", "b.py"] and plan.total_bytes == 30
    assert plan.skipped == (SkippedEntry("l", "symlink"), SkippedEntry("s", "submodule"))
    for unsupported in (TreeEntry("t", "040000", "tree", "3" * 40, None),
                        TreeEntry("m", "100664", "blob", "4" * 40, 1)):  # fmt: skip
        with pytest.raises(UnsafeTreeError, match="unsupported"):
            plan_tree([unsupported], RetrievalLimits())
    not_fetched = TreeEntry("big.bin", "100644", "blob", "5" * 40, None)  # left out by the filter
    with pytest.raises(RetrievalLimitError, match="larger than the limit") as raised:
        plan_tree([not_fetched], RetrievalLimits())
    assert raised.value.limit == "max_file_bytes"


# ---------------------------------------------------------------------------
# Blob streaming: lengths and ids are verified, not trusted


DATA = b"print('hi')\n"
OID = git_blob_id(DATA)
ENTRY = TreeEntry("a.py", "100644", "blob", OID, len(DATA))


def response(header: bytes, body: bytes = DATA) -> io.BytesIO:
    return io.BytesIO(header + body + b"\n")


def test_copy_blob() -> None:
    out = io.BytesIO()
    assert copy_blob(response(f"{OID} blob {len(DATA)}\n".encode()), ENTRY, out, "sha1", 100) == 12
    assert out.getvalue() == DATA


@pytest.mark.parametrize(
    ("stream", "error", "message"),
    [
        (response(f"{OID} missing\n".encode(), b""), RetrievalError, "unexpected object"),
        (response(f"{'0' * 40} blob 12\n".encode()), RetrievalError, "unexpected object"),
        (response(f"{OID} tree 12\n".encode()), RetrievalError, "unexpected object"),
        (
            response(f"{OID} blob 11\n".encode(), DATA[:11]),
            RetrievalError,
            "does not match the tree",
        ),
        (response(f"{OID} blob 101\n".encode()), RetrievalLimitError, "limit is 100"),
        (io.BytesIO(f"{OID} blob 12\n".encode() + DATA[:5]), RetrievalError, "truncated"),
        (response(f"{OID} blob 12\n".encode(), b"print('no')\n"), RetrievalError, "object id"),
        (io.BytesIO(f"{OID} blob 12\n".encode() + DATA + b"X"), RetrievalError, "malformed"),
    ],
)
def test_copy_blob_rejects(stream: io.BytesIO, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        copy_blob(stream, ENTRY, io.BytesIO(), "sha1", 100)


# ---------------------------------------------------------------------------
# Timeouts


def fake_git(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Replace git with a Python script (which ignores git's arguments)."""
    monkeypatch.setattr(retrieval, "_git_command", lambda: (sys.executable, "-c", script))


@pytest.mark.parametrize(
    "limits",
    [RetrievalLimits(command_timeout=0.5), RetrievalLimits(command_timeout=300, deadline=0.5)],
)
def test_timeouts(
    origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch, limits: RetrievalLimits
) -> None:
    fake_git(monkeypatch, "import time; time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(RetrievalTimeoutError), retrieve(by_sha("1" * 40), limits=limits,
                                                        temp_dir=work):  # fmt: skip
        pass
    assert time.monotonic() - started < 15


def test_timeout_kills_child_processes(
    origin: Origin, work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "grandchild-survived"
    grandchild = f"import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).touch()"
    fake_git(monkeypatch, "import subprocess, sys, time; "
                          f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
                          "time.sleep(60)")  # fmt: skip
    with (
        pytest.raises(RetrievalTimeoutError),
        retrieve(by_sha("1" * 40), limits=RetrievalLimits(command_timeout=1), temp_dir=work),
    ):
        pass
    time.sleep(3)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# The storage limit stops a fetch that is still running


GROWING_FETCH = """
import os, subprocess, sys, time
args = sys.argv[1:]
if args[:1] == ["grandchild"]:
    time.sleep(2)
    open(args[1], "w").close()
elif "init" in args:
    os.makedirs(args[-1])
elif "fetch" in args:
    subprocess.Popen([sys.executable, __file__, "grandchild", MARKER])
    pack = os.path.join(args[args.index("--git-dir") + 1], "objects", "pack")
    os.makedirs(pack)
    with open(os.path.join(pack, "tmp_pack_incoming"), "wb") as out:
        while True:  # a pack that never stops arriving
            out.write(bytes(64 * 1024))
            out.flush()
            time.sleep(0.01)
else:
    sys.exit(1)
"""


def test_storage_limit_kills_a_running_fetch(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "grandchild-survived"
    script = tmp_path / "growing_fetch.py"
    script.write_text(GROWING_FETCH.replace("MARKER", repr(str(marker))), encoding="utf-8")
    monkeypatch.setattr(retrieval, "_git_command", lambda: (sys.executable, str(script)))
    reasons: list[str] = []
    kill = retrieval._Process.kill

    def recording_kill(process: retrieval._Process, reason: str) -> None:
        reasons.append(reason)
        kill(process, reason)

    monkeypatch.setattr(retrieval._Process, "kill", recording_kill)
    limits = RetrievalLimits(max_git_bytes=1024 * 1024, command_timeout=60)
    started = time.monotonic()
    with pytest.raises(RetrievalLimitError) as raised, retrieve(by_sha("1" * 40), limits=limits,
                                                                temp_dir=work):  # fmt: skip
        pass
    assert raised.value.limit == "max_git_bytes"
    assert reasons[:1] == ["storage"]  # killed while running, not by the check after exit
    assert time.monotonic() - started < 10  # the fake fetch never ends; its timeout is 60 s
    time.sleep(3)
    assert not marker.exists()  # its child process was killed too


# ---------------------------------------------------------------------------
# Blobs over max_file_bytes are left out of the fetch


def test_large_blobs_are_not_downloaded(origin: Origin, work: Path) -> None:
    sha = origin.commit({"small.py": b"x = 1\n", "assets/big.bin": noise(256 * 1024)})
    # Without the filter the 256 KiB blob would fail max_git_bytes during the fetch.
    limits = RetrievalLimits(max_file_bytes=1000, max_git_bytes=64 * 1024)
    with (
        pytest.raises(RetrievalLimitError, match=r"assets/big\.bin") as raised,
        retrieve(by_sha(sha), limits=limits, temp_dir=work),
    ):
        pass
    assert raised.value.limit == "max_file_bytes"


def test_servers_without_filtering_stay_bounded(origin: Origin, work: Path) -> None:
    origin.filtering(False)
    sha = origin.commit({"small.py": b"x = 1\n", "assets/big.bin": noise(256 * 1024)})
    small_store = RetrievalLimits(max_file_bytes=1000, max_git_bytes=64 * 1024)
    with (
        pytest.raises(RetrievalLimitError) as raised,
        retrieve(by_sha(sha), limits=small_store, temp_dir=work),
    ):
        pass
    assert raised.value.limit == "max_git_bytes"
    large_store = RetrievalLimits(max_file_bytes=1000, max_git_bytes=1024 * 1024)
    with (
        pytest.raises(RetrievalLimitError, match="262144 bytes") as raised,
        retrieve(by_sha(sha), limits=large_store, temp_dir=work),
    ):
        pass
    assert raised.value.limit == "max_file_bytes"


def test_a_blob_at_the_limit_is_fetched(origin: Origin, work: Path) -> None:
    sha = origin.commit({"exact.bin": noise(1000)})
    with retrieve(by_sha(sha), limits=RetrievalLimits(max_file_bytes=1000), temp_dir=work) as tree:
        assert files_under(tree.root) == {"exact.bin": noise(1000)}


# ---------------------------------------------------------------------------
# The git executable is never taken from the current directory


def plant_git(directory: Path, marker: Path) -> None:
    """Fake gits, under every name Windows or POSIX could resolve, that record running."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("git.bat", "git.cmd"):
        (directory / name).write_text(f'@echo off\r\ntype nul > "{marker}"\r\nexit /b 1\r\n')
    for name in ("git.com", "git.exe"):
        (directory / name).write_bytes(b"not an executable")
    script = directory / "git"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    script.chmod(0o755)


def real_git() -> str:
    assert GIT is not None
    return os.path.join(os.path.dirname(GIT), retrieval._GIT_EXECUTABLE)


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake gits in the current directory, in relative PATH entries and (as batch files)
    in an absolute PATH directory listed before the real git; returns the marker file
    that any of them creates when run."""
    marker = tmp_path / "planted-git-ran"
    checkout = tmp_path / "untrusted-checkout"
    plant_git(checkout, marker)
    plant_git(checkout / "bin", marker)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("git.bat", "git.cmd", "git.com"):
        shutil.copy(checkout / name, scripts / name)
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    path = ["", ".", "bin", str(scripts), os.path.dirname(real_git())]
    monkeypatch.setenv("PATH", os.pathsep.join(path))
    return marker


def test_the_standard_lookup_would_pick_a_planted_git(planted: Path) -> None:
    """What the regression tests below guard against."""
    found = shutil.which("git")
    assert found is not None and os.path.normcase(found) != os.path.normcase(real_git())


def test_git_is_found_only_in_absolute_path_directories(planted: Path) -> None:
    [command] = retrieval._git_command()
    assert os.path.normcase(command) == os.path.normcase(real_git())


def test_a_planted_git_never_runs(planted: Path, origin: Origin, work: Path) -> None:
    sha = origin.commit({"a.py": b"a\n"})
    with retrieve(by_sha(sha), temp_dir=work) as tree:
        assert files_under(tree.root) == {"a.py": b"a\n"}
    assert not planted.exists()


def test_batch_files_and_relative_entries_are_not_git(planted: Path, tmp_path: Path) -> None:
    only_untrusted = os.pathsep.join(["", ".", "bin", str(tmp_path / "scripts")])
    with pytest.raises(GitUnavailableError):
        retrieval._git_command({"PATH": only_untrusted})
    with pytest.raises(GitUnavailableError):
        retrieval._git_command({})


def test_absolute_path_entries(tmp_path: Path) -> None:
    absolute = str(tmp_path)
    entries = ["", ".", "..", "bin", "./bin", absolute]
    if sys.platform == "win32":
        entries += ["C:bin", "\\bin", f'"{absolute}"']
    assert retrieval._absolute_path_entries(os.pathsep.join(entries)) == [absolute] * (
        2 if sys.platform == "win32" else 1
    )


# ---------------------------------------------------------------------------
# GitHub (opt-in: XRAY_TEST_GITHUB=1)


HELLO_WORLD = "https://github.com/octocat/Hello-World"
HELLO_WORLD_COMMIT = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"


@pytest.mark.skipif(not os.environ.get("XRAY_TEST_GITHUB"), reason="XRAY_TEST_GITHUB not set")
@pytest.mark.parametrize(
    "target",
    [ingestion_target(HELLO_WORLD, commit_sha=HELLO_WORLD_COMMIT),
     ingestion_target(HELLO_WORLD, ref="master")],
)  # fmt: skip
def test_github(target: IngestionTarget, work: Path) -> None:
    with retrieve(target, temp_dir=work) as tree:
        assert tree.repository.slug == "github.com/octocat/hello-world"
        assert len(tree.commit_sha) == 40
        assert "README" in files_under(tree.root)


def test_an_expired_deadline_starts_nothing(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "_git_command", lambda: pytest.fail("git must not run"))
    expired = time.monotonic() - 1
    with pytest.raises(RetrievalTimeoutError), retrieve(by_sha("1" * 40), temp_dir=work,
                                                        ends_at=expired):  # fmt: skip
        pass
    with pytest.raises(RetrievalTimeoutError):
        resolve_commit(by_ref("main"), temp_dir=work, ends_at=expired)
    assert retrieval.deadline(RetrievalLimits(deadline=5)) - time.monotonic() <= 5
