"""A generated repository large enough that "return everything" would be visible.

2 000 Python files (1 000 in one directory), 3 functions each, all importing one
hub module. Every response stays one bounded page, and each endpoint runs the same
number of statements as on the golden fixture.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.analyzer import analyze
from xraymyrepo.api import Settings, create_app
from xraymyrepo.cim import RepositoryRef
from xraymyrepo.persistence import persist_snapshot

from ..conftest import reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = [
    pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set"),
    pytest.mark.slow,
]

API = "/api/v1"
WIDE = "big/wide"  # 1 000 files in one directory
HUB = "big/hub.py"
SHARED = "big/hub.py#shared"  # imports target the most specific node they resolve to


def generate(root: Path) -> Path:
    (root / "big").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "big"\n', encoding="utf-8")
    (root / "big" / "__init__.py").write_text("", encoding="utf-8")
    (root / HUB).write_text("def shared():\n    return 1\n", encoding="utf-8")
    body = "from big.hub import shared\n\n" + "".join(
        f"def f{i}():\n    return shared()\n\n" for i in range(3)
    )
    for d in ["wide", *(f"d{n:02}" for n in range(20))]:
        directory = root / "big" / d
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
        for f in range(1000 if d == "wide" else 50):
            (directory / f"m{f:04}.py").write_text(body, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def large(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, int]]:
    assert psycopg is not None and DATABASE_URL is not None
    doc = analyze(generate(tmp_path_factory.mktemp("large")),
                  repository=RepositoryRef(host="local", owner="tests", name="large"),
                  commit_sha="a" * 40)  # fmt: skip
    assert len(doc.nodes) > 8000
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        snapshot_id = persist_snapshot(conn, doc)
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as client:
        yield client, snapshot_id


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    assert psycopg is not None
    executed: list[str] = []
    original = psycopg.Cursor.execute

    def counting(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(query))
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counting)
    return executed


def get(client: TestClient, path: str, **params: Any) -> Any:
    response = client.get(API + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_wide_directory_is_paged(large: tuple[TestClient, int], statements: list[str]) -> None:
    client, sid = large
    keys: list[str] = []
    cursor = None
    pages = 0
    while True:
        statements.clear()
        page = get(client, f"/snapshots/{sid}/tree/children", parent=WIDE, limit=200,
                   **({"cursor": cursor} if cursor else {}))  # fmt: skip
        assert len(statements) == 5
        assert len(page["items"]) <= 200
        keys += [n["key"] for n in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 6  # 1 001 children (with __init__.py), 200 per page
    assert len(keys) == len(set(keys)) == 1001
    assert keys == sorted(keys)  # files only: key order


def test_hub_relationships_are_paged(large: tuple[TestClient, int]) -> None:
    client, sid = large
    detail = get(client, f"/snapshots/{sid}/nodes/by-key", key=SHARED)
    assert detail["relationship_counts"]["IMPORTS"]["incoming"] == 2000
    page = get(client, f"/snapshots/{sid}/relationships", node=SHARED, direction="in", limit=100)
    assert len(page["items"]) == 100 and page["next_cursor"]


def test_large_snapshot_responds_quickly(large: tuple[TestClient, int]) -> None:
    """A smoke budget, generous for CI machines: no request scans the whole snapshot
    more than the search does."""
    client, sid = large
    for path, params in [
        (f"/snapshots/{sid}", {}),
        (f"/snapshots/{sid}/tree/children", {"parent": WIDE}),
        (f"/snapshots/{sid}/nodes/by-key", {"key": SHARED}),
        (f"/snapshots/{sid}/relationships", {"node": SHARED}),
        (f"/snapshots/{sid}/search", {"q": "m0999"}),
    ]:
        started = time.perf_counter()
        get(client, path, **params)
        assert time.perf_counter() - started < 2.0, path
