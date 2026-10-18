"""The ingestion service against local repositories and an in-memory snapshot store.

The store stands in for ``find_snapshot``, ``persist_snapshot`` and ``get_repository``
with their contracts (identity keyed, ``SnapshotExistsError`` on conflict, owner and
name matched ignoring case). ``test_service_db.py`` runs the same flow against
PostgreSQL and the API.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psycopg")

from xraymyrepo.analyzer import ANALYZER_VERSION, AnalysisOptions, AnalyzerContractError, analyze
from xraymyrepo.cim import RepositoryRef, SnapshotDocument, SnapshotStatus
from xraymyrepo.cim.snapshot import SnapshotIdentity
from xraymyrepo.ingestion import (
    IngestionInputError,
    RetrievalLimitError,
    RetrievalLimits,
    RetrievalTimeoutError,
    SkippedEntry,
    service,
)
from xraymyrepo.ingestion import git as retrieval
from xraymyrepo.ingestion.service import IngestResult, ingest
from xraymyrepo.persistence import RepositoryIdentityError, SnapshotExistsError, StoredSnapshot
from xraymyrepo.persistence.queries import RepositoryRecord

from .test_git import GITLINK, Origin, Spec

URL = "https://github.com/acme/app"
REPO = RepositoryRef(host="github.com", owner="acme", name="app")
CONN: Any = object()  # the store ignores the connection

REGULAR: dict[str, bytes] = {
    "README.md": b"# app\n",
    "pyproject.toml": b'[project]\nname = "app"\nversion = "1.0"\n',
    "src/app/__init__.py": b"",
    "src/app/core.py": b"import os\n\nfrom . import util\n\n\ndef run():\n    return util.f()\n",
    "src/app/util.py": b"def f():\n    return 1\n",
}
SPEC: Spec = {**REGULAR, "docs": ("120000", b"README.md"), "vendor/lib": ("160000", GITLINK)}
SKIPPED = (SkippedEntry("docs", "symlink"), SkippedEntry("vendor/lib", "submodule"))


class Store:
    """``find_snapshot``/``persist_snapshot``/``get_repository`` over a dict."""

    def __init__(self) -> None:
        self.snapshots: dict[tuple[str, str, str, str], StoredSnapshot] = {}
        self.documents: dict[int, SnapshotDocument] = {}
        self.repositories: list[RepositoryRef] = []
        self.calls: list[str] = []

    def find(self, conn: Any, identity: SnapshotIdentity) -> StoredSnapshot | None:
        self.calls.append("find")
        return self.snapshots.get(identity.as_tuple())

    def persist(self, conn: Any, document: SnapshotDocument) -> int:
        self.calls.append("persist")
        key = document.snapshot.as_tuple()
        if key in self.snapshots:
            raise SnapshotExistsError(*self.snapshots[key])
        snapshot_id = len(self.snapshots) + 1
        self.snapshots[key] = StoredSnapshot(snapshot_id, SnapshotStatus.COMPLETE)
        self.documents[snapshot_id] = document
        return snapshot_id

    def get_repository(self, conn: Any, host: str, owner: str, name: str) -> Any:
        self.calls.append("get_repository")
        for repository in self.repositories:
            if (repository.host, repository.owner.lower(), repository.name.lower()) == (
                host, owner.lower(), name.lower()
            ):  # fmt: skip
                return RepositoryRecord(repository, 0, None)
        return None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    fake = Store()
    monkeypatch.setattr(service, "find_snapshot", fake.find)
    monkeypatch.setattr(service, "persist_snapshot", fake.persist)
    monkeypatch.setattr(service, "get_repository", fake.get_repository)
    return fake


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


def forbid(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Fail the test if the service calls any of these (``retrieve``, ``resolve_commit``)."""
    for name in names:

        def called(*_: object, _name: str = name, **__: object) -> Any:
            raise AssertionError(f"{_name} must not be called")

        monkeypatch.setattr(service, name, called)


def local_analysis(tmp_path: Path, sha: str) -> SnapshotDocument:
    """The analyzer on a plain directory holding the commit's regular files."""
    root = tmp_path / "checkout"
    for path, data in REGULAR.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    return analyze(root, repository=REPO, commit_sha=sha)


# ---------------------------------------------------------------------------
# Success and identity


def test_ingest_retrieves_analyzes_and_persists(
    store: Store, origin: Origin, work: Path, tmp_path: Path
) -> None:
    sha = origin.commit(SPEC)
    result = ingest(CONN, URL, ref="main", temp_dir=work)
    assert result == IngestResult(1, REPO, sha, ANALYZER_VERSION,
                                  AnalysisOptions().config().config_hash(), False,
                                  SKIPPED)  # fmt: skip
    assert store.calls == ["get_repository", "find", "persist"]
    document = store.documents[1]
    assert document.snapshot == result.identity
    keys = {node.key for node in document.nodes}
    assert {"src/app/core.py", "src/app/util.py", "README.md"} <= keys
    assert not {"docs", "vendor", "vendor/lib"} & keys  # skipped entries are not represented
    # Exactly what the analyzer makes of a clean checkout of the commit's files.
    assert document.canonical() == local_analysis(tmp_path, sha).canonical()


def test_url_spelling_is_normalized(store: Store, origin: Origin, work: Path) -> None:
    sha = origin.commit(SPEC)
    result = ingest(CONN, "https://GitHub.com/ACME/App.git/", commit_sha=sha.upper(),
                    temp_dir=work)  # fmt: skip
    assert result.repository == REPO and result.commit_sha == sha
    assert store.documents[1].snapshot.repository == REPO


def test_identity_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, origin: Origin, work: Path
) -> None:
    """A ref and its commit, ingested into separate stores, give one identity and one
    document."""
    sha = origin.commit(SPEC)
    results, documents = [], []
    targets: list[dict[str, Any]] = [{"ref": "main"}, {"commit_sha": sha},
                                     {"ref": "refs/heads/main"}]  # fmt: skip
    for kwargs in targets:
        fake = Store()
        monkeypatch.setattr(service, "find_snapshot", fake.find)
        monkeypatch.setattr(service, "persist_snapshot", fake.persist)
        monkeypatch.setattr(service, "get_repository", fake.get_repository)
        results.append(ingest(CONN, URL, temp_dir=work, **kwargs))
        documents.append(fake.documents[1].to_canonical_json())
    assert len({r.identity for r in results}) == 1 and len(set(documents)) == 1


# ---------------------------------------------------------------------------
# Repeated and concurrent ingestion


def test_repeat_ingestion_returns_the_stored_snapshot_without_retrieving(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = origin.commit(SPEC)
    first = ingest(CONN, URL, ref="main", temp_dir=work)
    forbid(monkeypatch, "retrieve")
    again = ingest(CONN, URL, ref="main", temp_dir=work)
    by_sha = ingest(CONN, "https://github.com/Acme/App", commit_sha=sha, temp_dir=work)
    for result in (again, by_sha):
        assert result.snapshot_id == first.snapshot_id and result.identity == first.identity
        assert result.already_existed and result.skipped is None  # not retrieved: unknown
    assert store.calls.count("persist") == 1


def test_stored_commit_needs_no_git_at_all(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = origin.commit(SPEC)
    ingest(CONN, URL, commit_sha=sha, temp_dir=work)
    monkeypatch.setattr(retrieval, "_git_command", lambda: pytest.fail("git must not run"))
    assert ingest(CONN, URL, commit_sha=sha, temp_dir=work).already_existed


def test_concurrent_ingestion_that_persisted_first(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity was not stored when checked, but is by the time this ingestion
    persists: the winner's snapshot is the result."""
    origin.commit(SPEC)
    monkeypatch.setattr(service, "find_snapshot", lambda conn, identity: None)
    winner = ingest(CONN, URL, ref="main", temp_dir=work)
    loser = ingest(CONN, URL, ref="main", temp_dir=work)
    assert not winner.already_existed and loser.already_existed
    assert loser.snapshot_id == winner.snapshot_id and loser.identity == winner.identity
    assert loser.skipped == SKIPPED  # this call did retrieve the commit
    assert len(store.documents) == 1


@pytest.mark.parametrize("status", [SnapshotStatus.PENDING, SnapshotStatus.FAILED])
@pytest.mark.parametrize("found", [True, False], ids=["found-first", "found-on-persist"])
def test_incomplete_stored_snapshots_are_not_success(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch,
    status: SnapshotStatus, found: bool,
) -> None:  # fmt: skip
    sha = origin.commit(SPEC)
    identity = SnapshotIdentity(repository=REPO, commit_sha=sha, extractor_version=ANALYZER_VERSION,
                                config_hash=AnalysisOptions().config().config_hash())  # fmt: skip
    store.snapshots[identity.as_tuple()] = StoredSnapshot(7, status)
    if found:
        forbid(monkeypatch, "retrieve")
    else:
        monkeypatch.setattr(service, "find_snapshot", lambda conn, identity: None)
    with pytest.raises(SnapshotExistsError) as raised:
        ingest(CONN, URL, commit_sha=sha, temp_dir=work)
    assert (raised.value.snapshot_id, raised.value.status) == (7, status)


# ---------------------------------------------------------------------------
# Failures: nothing is persisted and the temporary directory is always removed


def test_invalid_input_touches_nothing(
    store: Store, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid(monkeypatch, "resolve_commit", "retrieve")
    invalid: list[dict[str, Any]] = [
        {"url": "http://github.com/acme/app", "ref": "main"}, {"url": URL},
        {"url": URL, "ref": "main", "commit_sha": "a" * 40},
        {"url": URL, "commit_sha": "abc1234"}, {"url": URL, "ref": "-x"},
    ]  # fmt: skip
    for kwargs in invalid:
        with pytest.raises(IngestionInputError):
            ingest(CONN, temp_dir=work, **kwargs)
    assert store.calls == []


def test_repository_stored_with_other_letter_case_fails_before_retrieval(
    store: Store, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.repositories.append(RepositoryRef(host="github.com", owner="Acme", name="App"))
    forbid(monkeypatch, "resolve_commit", "retrieve")
    with pytest.raises(RepositoryIdentityError, match=r"github\.com/Acme/App"):
        ingest(CONN, "https://github.com/acme/app", ref="main", temp_dir=work)
    assert store.calls == ["get_repository"]


def test_repository_stored_with_the_same_spelling_is_fine(
    store: Store, origin: Origin, work: Path
) -> None:
    origin.commit(SPEC)
    store.repositories.append(REPO)
    assert not ingest(
        CONN, "https://github.com/ACME/APP", ref="main", temp_dir=work
    ).already_existed


def test_retrieval_failure(store: Store, origin: Origin, work: Path) -> None:
    origin.commit(SPEC)
    with pytest.raises(RetrievalLimitError):
        ingest(CONN, URL, ref="main", limits=RetrievalLimits(max_files=2), temp_dir=work)
    assert "persist" not in store.calls


def failing_analyzer(*_: object, **__: object) -> SnapshotDocument:
    raise AnalyzerContractError("analyzer output violates CIM v1: boom")


def test_analyzer_failure(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit(SPEC)
    monkeypatch.setattr(service, "analyze", failing_analyzer)
    with pytest.raises(AnalyzerContractError, match="boom"):
        ingest(CONN, URL, ref="main", temp_dir=work)
    assert "persist" not in store.calls


def test_analyzer_output_with_another_identity_is_not_persisted(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit(SPEC)
    real: Callable[..., SnapshotDocument] = analyze

    def wrong_commit(root: Path, **kwargs: Any) -> SnapshotDocument:
        return real(root, **{**kwargs, "commit_sha": "f" * 40})

    monkeypatch.setattr(service, "analyze", wrong_commit)
    with pytest.raises(AnalyzerContractError, match="expected identity"):
        ingest(CONN, URL, ref="main", temp_dir=work)
    assert "persist" not in store.calls


def test_persistence_failure(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit(SPEC)

    def database_down(conn: Any, document: SnapshotDocument) -> int:
        raise ConnectionError("database went away")

    monkeypatch.setattr(service, "persist_snapshot", database_down)
    with pytest.raises(ConnectionError):  # not reported as an existing snapshot
        ingest(CONN, URL, ref="main", temp_dir=work)


def test_the_checkout_is_removed_before_persisting(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit(SPEC)

    def persist(conn: Any, document: SnapshotDocument) -> int:
        assert list(work.iterdir()) == []
        return store.persist(conn, document)

    monkeypatch.setattr(service, "persist_snapshot", persist)
    ingest(CONN, URL, ref="main", temp_dir=work)


# ---------------------------------------------------------------------------
# One deadline for resolving and retrieving


def test_resolution_and_retrieval_share_one_deadline(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit(SPEC)
    ends: list[float] = []
    session_init = retrieval._Session.__init__

    def recording_init(self: Any, *args: Any) -> None:
        session_init(self, *args)
        ends.append(self.end)

    monkeypatch.setattr(retrieval._Session, "__init__", recording_init)
    started = time.monotonic()
    ingest(CONN, URL, ref="main", limits=RetrievalLimits(deadline=120), temp_dir=work)
    finished = time.monotonic()
    assert len(ends) == 2 and ends[0] == ends[1]  # the resolving and the retrieving session
    assert started + 120 <= ends[0] <= finished + 120  # taken once, during this ingestion


def test_time_spent_resolving_counts_against_retrieval(
    store: Store, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution uses up the deadline: retrieval then times out at once instead of
    starting over with a fresh one, and runs no git command."""
    origin.commit(SPEC)
    real_resolve = retrieval.resolve_commit

    def slow_resolve(*args: Any, ends_at: float, **kwargs: Any) -> str:
        sha = real_resolve(*args, ends_at=ends_at, **kwargs)
        time.sleep(max(0.0, ends_at - time.monotonic()) + 0.05)
        return sha

    monkeypatch.setattr(service, "resolve_commit", slow_resolve)
    monkeypatch.setattr(retrieval, "_git_command", lambda: pytest.fail("git must not run"))
    with pytest.raises(RetrievalTimeoutError):
        ingest(CONN, URL, commit_sha="1" * 40, limits=RetrievalLimits(deadline=2), temp_dir=work)
    assert "persist" not in store.calls
