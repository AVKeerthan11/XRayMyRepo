"""``xray ingest`` and the ``xray`` dispatcher, against local repositories and the
in-memory snapshot store of ``test_service.py`` (no database, no GitHub)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")

from xraymyrepo import cli as xray  # noqa: E402
from xraymyrepo.analyzer import AnalyzerContractError  # noqa: E402
from xraymyrepo.cim import SnapshotStatus  # noqa: E402
from xraymyrepo.ingestion import (  # noqa: E402
    CommitNotFoundError,
    GitUnavailableError,
    RefResolutionError,
    RemoteUnavailableError,
    RetrievalLimitError,
    RetrievalTimeoutError,
    UnsafeTreeError,
    service,
)
from xraymyrepo.ingestion import cli as ingest_cli  # noqa: E402
from xraymyrepo.ingestion import git as retrieval  # noqa: E402
from xraymyrepo.persistence import RepositoryIdentityError, SnapshotExistsError  # noqa: E402

from ..analyzer.helpers import COMMIT, write_repo  # noqa: E402
from .test_git import Origin  # noqa: E402
from .test_service import REGULAR, SPEC, URL, Store  # noqa: E402

SECRET = "s3cret-password"
DATABASE_URL = f"postgresql://xray:{SECRET}@db.example.invalid:5432/xray"
EXPECTED_KEYS = ["snapshot_id", "repository", "commit_sha", "extractor_version", "config_hash",
                 "already_existed", "skipped"]  # fmt: skip


class FakeConnection:
    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    fake = Store()
    monkeypatch.setattr(service, "find_snapshot", fake.find)
    monkeypatch.setattr(service, "persist_snapshot", fake.persist)
    monkeypatch.setattr(service, "get_repository", fake.get_repository)
    return fake


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``XRAY_DATABASE_URL`` is set; connecting records the URL and needs no server."""
    opened: list[str] = []

    def connect(url: str) -> Any:
        opened.append(url)
        return FakeConnection()

    monkeypatch.setenv("XRAY_DATABASE_URL", DATABASE_URL)
    monkeypatch.setattr(ingest_cli, "_connect", connect)
    return opened


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    repo = Origin(tmp_path / "origin.git")
    monkeypatch.setattr(retrieval, "_remote_for", lambda _: retrieval._Remote(repo.url, "file"))
    return repo


@pytest.fixture
def temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The system temporary directory, as the command uses it; empty again afterwards."""
    path = tmp_path / "system-temp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    yield path
    assert list(path.iterdir()) == [], "the temporary directory was not removed"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = ingest_cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def assert_safe(text: str, temp: Path) -> None:
    assert SECRET not in text and "db.example.invalid" not in text
    assert str(temp) not in text and str(Path(tempfile.gettempdir())) not in text


# ---------------------------------------------------------------------------
# Success


def test_ingest_prints_one_json_line(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    sha = origin.commit(SPEC)
    code, out, err = run(capsys, URL, "--ref", "main")
    assert code == 0 and out.endswith("\n") and out.count("\n") == 1
    result = json.loads(out)
    assert list(result) == EXPECTED_KEYS
    assert result["snapshot_id"] == 1 and result["commit_sha"] == sha
    assert result["repository"] == {"host": "github.com", "owner": "acme", "name": "app"}
    assert result["already_existed"] is False
    assert result["config_hash"].startswith("sha256:") and result["extractor_version"]
    assert result["skipped"] == [{"path": "docs", "reason": "symlink"},
                                 {"path": "vendor/lib", "reason": "submodule"}]  # fmt: skip
    assert err == (
        "xray ingest: note: 2 tree entries are not represented in the snapshot: "
        "symlink 'docs', submodule 'vendor/lib'\n"
    )
    assert connections == [DATABASE_URL]
    assert_safe(out + err, temp)


def test_repeat_ingestion_reports_the_existing_snapshot(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    sha = origin.commit(SPEC)
    first = json.loads(run(capsys, URL, "--ref", "main")[1])
    code, out, err = run(capsys, "https://github.com/Acme/App.git", "--commit-sha", sha.upper())
    again = json.loads(out)
    assert code == 0 and err == ""
    assert again["snapshot_id"] == first["snapshot_id"] and again["already_existed"] is True
    assert again["skipped"] is None  # not retrieved this time: unknown, not empty
    assert {k: v for k, v in again.items() if k not in ("already_existed", "skipped")} == {
        k: v for k, v in first.items() if k not in ("already_existed", "skipped")
    }


def test_nothing_skipped(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    origin.commit(REGULAR)
    code, out, err = run(capsys, URL, "--ref", "main")
    assert code == 0 and json.loads(out)["skipped"] == [] and err == ""


def test_many_skipped_entries_are_summarized(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    origin.commit({**REGULAR, **{f"links/l{i:02}": ("120000", b"README.md") for i in range(12)}})
    code, out, err = run(capsys, URL, "--ref", "main")
    assert code == 0 and len(json.loads(out)["skipped"]) == 12
    assert err.startswith("xray ingest: note: 12 tree entries") and err.endswith(", and 2 more\n")


# ---------------------------------------------------------------------------
# Arguments and input


@pytest.mark.parametrize(
    "argv",
    [[], ["--ref", "main"], [URL], [URL, "--ref", "main", "--commit-sha", COMMIT],
     [URL, "--ref"], [URL, "--database-url", DATABASE_URL, "--ref", "main"]],
)  # fmt: skip
def test_usage_errors(
    argv: list[str], connections: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        ingest_cli.main(argv)
    assert raised.value.code == 2
    out, err = capsys.readouterr()
    assert out == "" and "usage: xray ingest" in err and SECRET not in err
    assert connections == []


@pytest.mark.parametrize(
    "argv",
    [["http://github.com/acme/app", "--ref", "main"],
     [f"https://user:{SECRET}@github.com/acme/app", "--ref", "main"],
     [f"https://github.com/acme/app?token={SECRET}", "--ref", "main"],
     ["https://gitlab.com/acme/app", "--ref", "main"],
     [URL, "--commit-sha", "abc1234"], [URL, "--ref=-x"], [URL, "--ref", "a..b"],
     [URL, "--ref", ""]],
)  # fmt: skip
def test_invalid_input_never_connects(
    argv: list[str], connections: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, err = run(capsys, *argv)
    assert (code, out) == (2, "") and err.startswith("xray ingest: invalid_input: ")
    assert err.count("\n") == 1 and connections == []
    assert_safe(err, tmp_path)


def test_database_url_must_be_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("XRAY_DATABASE_URL", raising=False)
    code, out, err = run(capsys, URL, "--ref", "main")
    assert (code, out) == (
        2,
        "",
    ) and err == "xray ingest: configuration: XRAY_DATABASE_URL is not set\n"


@pytest.mark.parametrize(
    "database_url",
    [f"postgresql://xray:{SECRET}@127.0.0.1:1/xray?connect_timeout=5",  # nothing listens
     f"host=db.example.invalid password={SECRET} not-a-keyword"],  # malformed, echoed by libpq
)  # fmt: skip
def test_connection_failures_never_print_the_database_url(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:  # fmt: skip
    monkeypatch.setenv("XRAY_DATABASE_URL", database_url)
    code, out, err = run(capsys, URL, "--ref", "main")
    assert (code, out) == (6, "")
    assert err == (
        "xray ingest: database_unavailable: cannot connect to the database in XRAY_DATABASE_URL\n"
    )
    assert_safe(err, tmp_path)


# ---------------------------------------------------------------------------
# Failures of the service map to exit codes; only safe messages are printed


@pytest.mark.parametrize(
    ("error", "exit_code", "code"),
    [
        (GitUnavailableError("git is not installed or not on PATH"), 3, "retrieval_failed"),
        (RemoteUnavailableError("the repository could not be fetched"), 3, "retrieval_failed"),
        (RefResolutionError("ref 'main' does not exist in the repository"), 3, "retrieval_failed"),
        (CommitNotFoundError("commit does not exist"), 3, "retrieval_failed"),
        (UnsafeTreeError("path has a .git component: 'a/.git'"), 3, "retrieval_failed"),
        (RetrievalLimitError("max_files", "the tree has 3 entries; the limit is 2"), 4,
         "limit_exceeded"),
        (RetrievalTimeoutError("a git command ran out of time"), 4, "timeout"),
        (AnalyzerContractError("analyzer output violates CIM v1: x\ndetails"), 5,
         "analysis_failed"),
        (RepositoryIdentityError("repository 'github.com/acme/app' is stored as ..."), 6,
         "database_error"),
        (SnapshotExistsError(3, SnapshotStatus.PENDING), 6, "database_error"),
        (psycopg.errors.UniqueViolation(f"duplicate key; password={SECRET}"), 6, "database_error"),
        (psycopg.OperationalError(f"server closed the connection {SECRET}"), 6, "database_error"),
        (RuntimeError(f"boom in {tempfile.gettempdir()} with {SECRET}"), 1, "unexpected_error"),
        (OSError(f"cannot read {tempfile.gettempdir()}/xray-ingest-1/tree/a.py"), 1,
         "unexpected_error"),
    ],
)  # fmt: skip
def test_service_failures(
    error: Exception, exit_code: int, code: str, connections: list[str],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:  # fmt: skip
    def failing_ingest(*args: object, **kwargs: object) -> Any:
        raise error

    monkeypatch.setattr(ingest_cli, "ingest", failing_ingest)
    result = run(capsys, URL, "--ref", "main")
    assert result[:2] == (exit_code, "")
    err = result[2]
    assert err.startswith(f"xray ingest: {code}: ") and err.count("\n") == 1
    assert_safe(err, tmp_path)


def test_real_retrieval_failure(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    origin.commit(SPEC)
    code, out, err = run(capsys, URL, "--ref", "no-such-branch")
    assert (code, out) == (3, "")
    assert err == (
        "xray ingest: retrieval_failed: ref 'no-such-branch' does not exist in the repository\n"
    )
    assert "persist" not in store.calls


def test_real_limit_failure(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    origin.commit({**REGULAR, "big.bin": b"x" * 2048})
    real_ingest = service.ingest
    monkeypatch.setattr(ingest_cli, "ingest", lambda *a, **k: real_ingest(
        *a, **k, limits=retrieval.RetrievalLimits(max_file_bytes=1024)))  # fmt: skip
    code, out, err = run(capsys, URL, "--ref", "main")
    assert (code, out) == (4, "")
    assert err.startswith("xray ingest: limit_exceeded: ") and "(max_file_bytes)" in err
    assert_safe(err, temp)


# ---------------------------------------------------------------------------
# The xray dispatcher


def test_xray_dispatches_ingest(
    store: Store, connections: list[str], origin: Origin, temp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    origin.commit(SPEC)
    assert xray.main(["ingest", URL, "--ref", "main"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == 1


def test_xray_analyze_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = write_repo(tmp_path / "repo", {"m.py": "import json\n"})
    assert (
        xray.main(["analyze", str(repo), "--commit", COMMIT, "--repository", "github.com/o/r"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["snapshot"]["commit_sha"] == COMMIT
    assert xray.main(["analyze", str(tmp_path / "missing"), "--commit", COMMIT]) == 1


@pytest.mark.parametrize(
    ("argv", "status"), [([], 2), (["bogus"], 2), (["-h"], 0), (["--help"], 0)]
)
def test_xray_usage(argv: list[str], status: int, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        xray.main(argv)
    assert raised.value.code == status
    out = capsys.readouterr().out
    if status == 0:
        assert "analyze" in out and "ingest" in out


def test_python_dash_m_xraymyrepo() -> None:
    result = subprocess.run([sys.executable, "-m", "xraymyrepo", "ingest", "--help"],
                            capture_output=True, text=True, check=False)  # fmt: skip
    assert result.returncode == 0 and "XRAY_DATABASE_URL" in result.stdout
    assert "--commit-sha" in result.stdout and "--ref" in result.stdout
