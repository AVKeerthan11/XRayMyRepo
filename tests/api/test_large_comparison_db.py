"""Comparing two generated snapshots of about 8 000 nodes each: measured, not budgeted.

2 000 Python files (40 directories of 50), 3 functions each, all importing one hub
module; every file's content is unique. The second state edits 100 files, moves 50
unchanged (into a new directory), removes 50 and adds 50.

The tests check correctness and that API requests stay a fixed number of statements;
timings are measured and printed (run with ``-s``), never asserted: there are no
agreed targets. Phases measured: the pure comparison, storing it (id lookups, bulk
copy, the completion check of migration 0004 and the commit, which runs the deferred
reference checks), loading it back, the whole orchestration, and API pages.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.analyzer import analyze
from xraymyrepo.api import Settings, create_app
from xraymyrepo.cim import NodeChangeKind, RepositoryRef, SnapshotComparison, SnapshotDocument
from xraymyrepo.insights.comparison import ComparisonParams, compare

from ..conftest import reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = [
    pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set"),
    pytest.mark.slow,
]

if DATABASE_URL:
    from xraymyrepo.orchestration import compare_snapshots
    from xraymyrepo.persistence import comparisons as cp
    from xraymyrepo.persistence import (
        load_comparison,
        load_snapshot,
        persist_comparison,
        persist_snapshot,
    )

API = "/api/v1"
REPO = RepositoryRef(host="local", owner="tests", name="large-comparison")
DIRECTORIES = 40
FILES = 50
EDITED = range(0, 100)  # d00, d01
MOVED_DIR = "d02"  # -> big/moved
REMOVED_DIR = "d03"
ADDED = 50  # big/new/n00..n49

MEASURED: dict[str, float] = {}


def measure[T](name: str, run: Callable[[], T]) -> T:
    started = time.perf_counter()
    result = run()
    MEASURED[name] = time.perf_counter() - started
    return result


def module(value: int, edited: bool = False) -> str:
    extra = " + 1" if edited else ""
    return (
        f"from big.hub import shared\n\nVALUE = {value}\n\n"
        f"def f0():\n    return shared() + VALUE{extra}\n\n"
        "def f1():\n    return shared()\n\n"
        "def f2():\n    return VALUE\n"
    )


def generate(root: Path, *, after: bool) -> Path:
    big = root / "big"
    big.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "big"\n', encoding="utf-8")
    (big / "__init__.py").write_text("", encoding="utf-8")
    (big / "hub.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    for d in range(DIRECTORIES):
        name = f"d{d:02}"
        if after and name == REMOVED_DIR:
            continue
        directory = big / ("moved" if after and name == MOVED_DIR else name)
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
        for f in range(FILES):
            index = d * FILES + f
            source = module(index, edited=after and index in EDITED)
            (directory / f"m{f:02}.py").write_text(source, encoding="utf-8")
    if after:
        (big / "new").mkdir()
        (big / "new" / "__init__.py").write_text("", encoding="utf-8")
        for n in range(ADDED):
            (big / "new" / f"n{n:02}.py").write_text(module(10_000 + n), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def docs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SnapshotDocument]:
    root = tmp_path_factory.mktemp("large-comparison")
    found = {}
    for name, sha in (("before", "a" * 40), ("after", "b" * 40)):
        path = generate(root / name, after=name == "after")
        found[name] = measure(
            f"analyze {name}", partial(analyze, path, repository=REPO, commit_sha=sha)
        )
    assert all(len(d.nodes) > 8000 for d in found.values()), [len(d.nodes) for d in found.values()]
    return found


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        reset_database(connection)
        yield connection


@pytest.fixture(scope="module")
def ids(conn: Any, docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    found = {}
    for name, doc in docs.items():
        found[name] = measure(f"persist snapshot {name}", partial(persist_snapshot, conn, doc))
    return found


@pytest.fixture(scope="module")
def comparison(docs: dict[str, SnapshotDocument]) -> SnapshotComparison:
    return measure("compare (in memory)", lambda: compare(docs["before"], docs["after"]))


@pytest.fixture(scope="module")
def stored(conn: Any, ids: dict[str, int], comparison: SnapshotComparison) -> int:
    """Store the comparison, timing its phases: statements by kind, the bulk copy, and
    the commit (deferred reference checks) as the remainder."""
    assert psycopg is not None
    phases: dict[str, float] = {}
    original_execute = psycopg.Cursor.execute
    original_copy = cp._copy

    def timed_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(query)
        phase = ("complete (migration 0004 completion check)" if "status = 'complete'" in sql
                 else "node id lookup" if "FROM node WHERE" in sql
                 else "edge id lookup" if "FROM edge e" in sql
                 else "other statements")  # fmt: skip
        started = time.perf_counter()
        try:
            return original_execute(self, query, *args, **kwargs)
        finally:
            phases[phase] = phases.get(phase, 0.0) + time.perf_counter() - started

    def timed_copy(cur: Any, statement: str, rows: Any) -> None:
        started = time.perf_counter()
        original_copy(cur, statement, rows)
        table = statement.split()[1]
        phases[f"COPY {table}"] = time.perf_counter() - started

    mp = pytest.MonkeyPatch()
    mp.setattr(psycopg.Cursor, "execute", timed_execute)
    mp.setattr(cp, "_copy", timed_copy)
    try:
        comparison_id = measure("persist comparison (total)", lambda: persist_comparison(
            conn, ids["before"], ids["after"], comparison,
            ComparisonParams().as_json()))  # fmt: skip
    finally:
        mp.undo()
    for phase, seconds in phases.items():
        MEASURED[f"  persist: {phase}"] = seconds
    MEASURED["  persist: commit + Python (remainder)"] = MEASURED[
        "persist comparison (total)"
    ] - sum(phases.values())
    return comparison_id


@pytest.fixture(scope="module", autouse=True)
def report() -> Iterator[None]:
    yield
    if MEASURED:
        width = max(map(len, MEASURED))
        print("\nLarge comparison measurements (seconds):")
        for name, seconds in MEASURED.items():
            print(f"  {name:<{width}}  {seconds:8.3f}")


# ---------------------------------------------------------------------------


def test_the_comparison_finds_the_generated_changes(
    docs: dict[str, SnapshotDocument], comparison: SnapshotComparison
) -> None:
    counts = comparison.summary().nodes
    print(f"\nnodes before={len(docs['before'].nodes)} after={len(docs['after'].nodes)} "
          f"node rows={len(comparison.nodes)} edge rows={len(comparison.edges)} "
          f"unchanged edges={comparison.unchanged_edges}")  # fmt: skip
    print({k.value: v for k, v in counts.items()})
    assert counts[NodeChangeKind.MOVED] == FILES * 4  # each file and its 3 functions
    assert counts[NodeChangeKind.MODIFIED] >= len(EDITED) * 2  # the file and f0
    assert counts[NodeChangeKind.REMOVED] >= FILES * 4
    assert counts[NodeChangeKind.ADDED] >= ADDED * 4
    assert counts[NodeChangeKind.UNDETERMINED] == 0


def test_store_and_reload(conn: Any, stored: int, comparison: SnapshotComparison) -> None:
    loaded = measure("load comparison", lambda: load_comparison(conn, stored))
    assert loaded == comparison


def test_orchestration_end_to_end(conn: Any, ids: dict[str, int], stored: int) -> None:
    """Load both snapshots, compare, store: another identity (other params)."""
    params = ComparisonParams(min_move_symbols=2)
    measure("load_snapshot (one side)", partial(load_snapshot, conn, ids["before"]))
    run = partial(compare_snapshots, conn, ids["before"], ids["after"], params)
    other = measure("compare_snapshots (load x2 + compare + persist)", run)
    assert other != stored


@pytest.fixture(scope="module")
def client(stored: int) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as test_client:
        yield test_client


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


def test_api_pages(client: TestClient, stored: int, comparison: SnapshotComparison,
                   statements: list[str]) -> None:  # fmt: skip
    base = f"/comparisons/{stored}"
    requests: list[tuple[str, str, dict[str, Any], int]] = [
        ("GET comparison", base, {}, 5),
        ("GET summary", f"{base}/summary", {}, 2),
        ("GET node-changes (first page of 200)", f"{base}/node-changes", {"limit": 200}, 2),
        ("GET node-changes (change=modified)", f"{base}/node-changes",
         {"change": "modified", "limit": 200}, 2),
        ("GET node-changes (kind=file, change=moved)", f"{base}/node-changes",
         {"kind": "file", "change": "moved", "limit": 200}, 2),
        ("GET edge-changes (first page of 200)", f"{base}/edge-changes", {"limit": 200}, 2),
    ]  # fmt: skip
    get(client, f"{base}/summary")  # warm the connection pool: not measured
    for name, path, params, budget in requests:
        statements.clear()
        started = time.perf_counter()
        get(client, path, **params)
        MEASURED[name] = time.perf_counter() - started
        assert len(statements) == budget, (name, statements)

    def walk(path: str) -> list[Any]:
        items: list[Any] = []
        cursor = None
        while True:
            page = get(client, path, limit=1000, **({"cursor": cursor} if cursor else {}))
            items += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return items

    nodes = measure("walk all node-changes (pages of 1 000)", lambda: walk(f"{base}/node-changes"))
    assert [n["key"] for n in nodes] == [n.key for n in comparison.nodes]
    edges = measure("walk all edge-changes (pages of 1 000)", lambda: walk(f"{base}/edge-changes"))
    assert len(edges) == len(comparison.edges)
