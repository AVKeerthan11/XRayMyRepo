"""Change risk of two generated snapshots of about 8 000 nodes each: measured, not budgeted.

2 000 Python files (40 directories of 50), 3 functions each, every one importing one hub
module; the 50 files of ``d04`` also import ``big/lib.py``. The second state edits the
hub (2 000 dependents) and 100 files, moves 50 unchanged, removes 50, adds 50, and
removes ``big/lib.py`` while its 50 importers stay unchanged (50 imports left dangling).

The tests check correctness; timings and memory are measured and printed (run with
``-s``), never asserted: there are no agreed targets. Memory is the peak of Python
allocations (``tracemalloc``) during a phase, measured in a separate pass because
tracing slows Python down.
"""

from __future__ import annotations

import os
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    ChangeRiskAssessment,
    RepositoryRef,
    RiskFactorRule,
    RiskLevel,
    SnapshotComparison,
    SnapshotDocument,
)
from xraymyrepo.insights.change_risk import ChangeRiskParams, change_risk

from .conftest import reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = [
    pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set"),
    pytest.mark.slow,
]

if DATABASE_URL:
    from xraymyrepo.orchestration import assess_change_risk, compare_snapshots
    from xraymyrepo.persistence import (
        load_change_risk,
        load_comparison,
        load_snapshot,
        persist_change_risk,
        persist_snapshot,
    )

REPO = RepositoryRef(host="local", owner="tests", name="large-change-risk")
DIRECTORIES, FILES = 40, 50
EDITED = range(0, 100)  # d00, d01
MOVED_DIR, REMOVED_DIR, LIB_DIR = "d02", "d03", "d04"
ADDED = 50

TIMES: dict[str, float] = {}
PEAKS: dict[str, float] = {}


def timed[T](name: str, run: Callable[[], T]) -> T:
    started = time.perf_counter()
    result = run()
    TIMES[name] = time.perf_counter() - started
    return result


def peak[T](name: str, run: Callable[[], T]) -> T:
    """Peak Python allocations (MiB) above what was allocated before ``run``."""
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        result = run()
        PEAKS[name] = (tracemalloc.get_traced_memory()[1] - base) / 2**20
    finally:
        tracemalloc.stop()
    return result


def module(value: int, *, edited: bool = False, lib: bool = False) -> str:
    extra = " + 1" if edited else ""
    imports = "from big.hub import shared\n" + ("from big.lib import helper\n" if lib else "")
    return (
        f"{imports}\nVALUE = {value}\n\n"
        f"def f0():\n    return shared() + VALUE{extra}\n\n"
        "def f1():\n    return shared()\n\n"
        "def f2():\n    return VALUE\n"
    )


def generate(root: Path, *, after: bool) -> Path:
    big = root / "big"
    big.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "big"\n', encoding="utf-8")
    (big / "__init__.py").write_text("", encoding="utf-8")
    hub = "def shared():\n    return 2\n" if after else "def shared():\n    return 1\n"
    (big / "hub.py").write_text(hub, encoding="utf-8")
    if not after:
        (big / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    for d in range(DIRECTORIES):
        name = f"d{d:02}"
        if after and name == REMOVED_DIR:
            continue
        directory = big / ("moved" if after and name == MOVED_DIR else name)
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
        for f in range(FILES):
            index = d * FILES + f
            source = module(index, edited=after and index in EDITED, lib=name == LIB_DIR)
            (directory / f"m{f:02}.py").write_text(source, encoding="utf-8")
    if after:
        (big / "new").mkdir()
        (big / "new" / "__init__.py").write_text("", encoding="utf-8")
        for n in range(ADDED):
            (big / "new" / f"n{n:02}.py").write_text(module(10_000 + n), encoding="utf-8")
    return root


@pytest.fixture(scope="module", autouse=True)
def report() -> Iterator[None]:
    yield
    width = max(map(len, [*TIMES, *PEAKS]), default=0)
    if TIMES:
        print("\nLarge change-risk timings (seconds):")
        for name, seconds in TIMES.items():
            print(f"  {name:<{width}}  {seconds:8.3f}")
    if PEAKS:
        print("Peak Python allocations during a phase (MiB, tracemalloc):")
        for name, mib in PEAKS.items():
            print(f"  {name:<{width}}  {mib:8.1f}")


@pytest.fixture(scope="module")
def docs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SnapshotDocument]:
    root = tmp_path_factory.mktemp("large-change-risk")
    found = {}
    for name, sha in (("before", "a" * 40), ("after", "b" * 40)):
        path = generate(root / name, after=name == "after")
        found[name] = analyze(path, repository=REPO, commit_sha=sha)
    assert all(len(d.nodes) > 8000 for d in found.values()), [len(d.nodes) for d in found.values()]
    return found


@pytest.fixture(scope="module")
def stored(docs: dict[str, SnapshotDocument]) -> Iterator[tuple[Any, int, dict[str, int]]]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        ids = {name: persist_snapshot(conn, doc) for name, doc in docs.items()}
        cid = compare_snapshots(conn, ids["before"], ids["after"])
        yield conn, cid, ids


@pytest.fixture(scope="module")
def loaded(stored: tuple[Any, int, dict[str, int]]
           ) -> tuple[SnapshotComparison, SnapshotDocument, SnapshotDocument]:  # fmt: skip
    conn, cid, ids = stored
    comparison = timed("load_comparison", lambda: load_comparison(conn, cid))
    before = timed("load_snapshot (from)", lambda: load_snapshot(conn, ids["before"]))
    after = timed("load_snapshot (to)", lambda: load_snapshot(conn, ids["after"]))
    return comparison, before, after


@pytest.fixture(scope="module")
def assessment(loaded: tuple[SnapshotComparison, SnapshotDocument, SnapshotDocument]
               ) -> ChangeRiskAssessment:  # fmt: skip
    return timed("change_risk (in memory)", lambda: change_risk(*loaded))


def item(a: ChangeRiskAssessment, key: str) -> Any:
    return next(i for i in a.items if i.subject_key == key)


def factor(i: Any, rule: RiskFactorRule) -> Any:
    return next(f for f in i.factors if f.rule is rule)


# ---------------------------------------------------------------------------


def test_the_assessment_finds_the_generated_risks(
    loaded: tuple[SnapshotComparison, SnapshotDocument, SnapshotDocument],
    assessment: ChangeRiskAssessment,
) -> None:
    comparison, before, after = loaded
    a = assessment
    print(f"\nnodes before={len(before.nodes)} after={len(after.nodes)} "
          f"node changes={len(comparison.nodes)} subjects={len(a.items)} "
          f"factors={sum(len(i.factors) for i in a.items)} "
          f"levels={ {k.value: v for k, v in a.summary().levels.items()} }")  # fmt: skip
    # The hub: 2 000 dependents (wide) and the inline health hub finding.
    hub = item(a, "big/hub.py")
    assert factor(hub, RiskFactorRule.IMPACT_DEPENDENTS).value >= 2000
    assert factor(hub, RiskFactorRule.IMPACT_DEPENDENTS).points == 2
    assert factor(hub, RiskFactorRule.HEALTH_HUB).points == 1
    assert hub.level is RiskLevel.HIGH
    # big/lib.py removed while its 50 importers stay: 50 exact unresolved imports.
    lib = item(a, "big/lib.py")
    dangling = factor(lib, RiskFactorRule.UNRESOLVED_AFTER_REMOVAL)
    assert (dangling.points, dangling.value, len(dangling.evidence)) == (2, 50, 20)  # sampled
    assert lib.level is RiskLevel.HIGH
    # 100 edited, 50 moved, 50 removed, 50 added files, the hub and lib.py, and four
    # __init__.py: empty blobs are never move-matched, so d02/ and d03/'s are removed
    # and moved/ and new/'s added.
    assert len(a.items) == 100 + 50 + 50 + 50 + 2 + 4
    assert a.excluded_by_origin == 0


def test_store_load_and_orchestrate(stored: tuple[Any, int, dict[str, int]],
                                    assessment: ChangeRiskAssessment) -> None:  # fmt: skip
    conn, cid, _ = stored
    params_json = ChangeRiskParams().as_json()
    rid = timed("persist_change_risk",
                lambda: persist_change_risk(conn, cid, assessment, params_json))  # fmt: skip
    assert timed("load_change_risk", lambda: load_change_risk(conn, rid)) == assessment
    params = ChangeRiskParams(wide_dependents=20)  # another identity
    other = timed("assess_change_risk (load x3 + assess + persist)",
                  lambda: assess_change_risk(conn, cid, params))  # fmt: skip
    assert other != rid


Loaded = tuple[SnapshotComparison, SnapshotDocument, SnapshotDocument]


def test_memory(stored: tuple[Any, int, dict[str, int]], loaded: Loaded) -> None:
    conn, cid, ids = stored
    peak("load_snapshot (one side)", lambda: load_snapshot(conn, ids["before"]))
    peak("change_risk (in memory)", lambda: change_risk(*loaded))
    peak("assess_change_risk (end to end)",
         lambda: assess_change_risk(conn, cid, ChangeRiskParams(wide_dependents=30)))  # fmt: skip
    assert PEAKS["change_risk (in memory)"] > 0
