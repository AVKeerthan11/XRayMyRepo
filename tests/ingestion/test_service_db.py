"""The ingestion service against a real PostgreSQL, read back through the API.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database (every
test drops and re-migrates its schema; see docs/persistence/postgres-v1.md). The remote
is a local bare repository; nothing contacts GitHub.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.cim import SnapshotDocument

from ..conftest import reset_database
from .test_git import Origin
from .test_service import SKIPPED, SPEC, URL, local_analysis

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from fastapi.testclient import TestClient

    from xraymyrepo.api import Settings, create_app
    from xraymyrepo.ingestion import git as retrieval
    from xraymyrepo.ingestion import service
    from xraymyrepo.ingestion.service import ingest
    from xraymyrepo.persistence import RepositoryIdentityError, load_snapshot, persist_snapshot

API = "/api/v1"


@pytest.fixture(scope="module")
def connection() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def conn(connection: Any) -> Any:
    reset_database(connection)
    return connection


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


def snapshot_count(conn: Any) -> int:
    row = conn.execute("SELECT count(*) FROM snapshot").fetchone()
    return int(row[0])


def test_ingested_snapshot_round_trips_and_is_served(
    conn: Any, origin: Origin, work: Path, tmp_path: Path
) -> None:
    sha = origin.commit(SPEC)
    result = ingest(conn, URL, ref="main", temp_dir=work)
    assert not result.already_existed and result.skipped == SKIPPED
    stored: SnapshotDocument = load_snapshot(conn, result.snapshot_id)
    assert stored == local_analysis(tmp_path, sha).canonical()

    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as client:
        detail = client.get(f"{API}/snapshots/{result.snapshot_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["repository"] == {"host": "github.com", "owner": "acme", "name": "app"}
        fields = ("commit_sha", "extractor_version", "config_hash", "status")
        assert tuple(body[f] for f in fields) == (
            sha, result.extractor_version, result.config_hash, "complete"
        )  # fmt: skip
        # Any spelling of the repository finds it (the API matches owner and name ignoring case).
        listing = client.get(f"{API}/repositories/github.com/Acme/App/snapshots")
        assert listing.status_code == 200, listing.text
        assert [s["id"] for s in listing.json()["items"]] == [result.snapshot_id]


def test_repeat_ingestion_reuses_the_snapshot(
    conn: Any, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = origin.commit(SPEC)
    first = ingest(conn, URL, ref="main", temp_dir=work)
    monkeypatch.setattr(service, "retrieve", lambda *a, **k: pytest.fail("must not retrieve"))
    again = ingest(conn, "https://github.com/ACME/app.git", commit_sha=sha, temp_dir=work)
    assert again.snapshot_id == first.snapshot_id and again.already_existed
    assert again.skipped is None and snapshot_count(conn) == 1


def test_lost_race_returns_the_winner(
    conn: Any, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``SnapshotExistsError`` from persistence, after the existence check missed."""
    origin.commit(SPEC)
    monkeypatch.setattr(service, "find_snapshot", lambda conn, identity: None)
    winner = ingest(conn, URL, ref="main", temp_dir=work)
    loser = ingest(conn, URL, ref="main", temp_dir=work)
    assert (winner.already_existed, loser.already_existed) == (False, True)
    assert loser.snapshot_id == winner.snapshot_id and snapshot_count(conn) == 1


def test_simultaneous_ingestions_store_one_snapshot(
    conn: Any, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ingestions on separate connections persist at the same moment."""
    assert psycopg is not None and DATABASE_URL is not None
    origin.commit(SPEC)
    barrier = threading.Barrier(2, timeout=60)
    persist = persist_snapshot

    def persist_together(connection: Any, document: SnapshotDocument) -> int:
        barrier.wait()
        return persist(connection, document)

    monkeypatch.setattr(service, "persist_snapshot", persist_together)
    results: list[Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        assert DATABASE_URL is not None and psycopg is not None
        try:
            with psycopg.connect(DATABASE_URL, autocommit=True) as own:
                results.append(ingest(own, URL, ref="main", temp_dir=work))
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(r.already_existed for r in results) == [False, True]
    assert len({r.snapshot_id for r in results}) == 1 and snapshot_count(conn) == 1


def test_repository_stored_with_other_letter_case(
    conn: Any, origin: Origin, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit(SPEC)
    conn.execute("INSERT INTO repository (host, owner, name) VALUES ('github.com', 'Acme', 'App')")
    with pytest.raises(RepositoryIdentityError):  # checked before retrieval
        ingest(conn, URL, ref="main", temp_dir=work)
    # Persistence enforces the same rule if the early check is bypassed; nothing is stored.
    monkeypatch.setattr(service, "get_repository", lambda *a: None)
    with pytest.raises(RepositoryIdentityError):
        ingest(conn, URL, ref="main", temp_dir=work)
    assert snapshot_count(conn) == 0


def test_cli_ingests_into_the_configured_database(
    conn: Any, origin: Origin, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from xraymyrepo.ingestion import cli

    assert DATABASE_URL is not None
    sha = origin.commit(SPEC)
    monkeypatch.setenv("XRAY_DATABASE_URL", DATABASE_URL)
    assert cli.main([URL, "--ref", "main"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["commit_sha"] == sha and first["already_existed"] is False
    assert cli.main([URL, "--commit-sha", sha]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["snapshot_id"] == first["snapshot_id"] and again["already_existed"] is True
    assert snapshot_count(conn) == 1
