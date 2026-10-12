"""Health runs in PostgreSQL: orchestration, persistence reuse and the new read queries.

Skipped unless ``XRAY_TEST_DATABASE_URL`` is set. Health runs reuse the derivation
tables (migration 0003) and ``persist_derivation`` unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

from xraymyrepo.cim import SnapshotDocument
from xraymyrepo.insights.health import HealthParams, health

from .conftest import reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo import orchestration
    from xraymyrepo.persistence import (
        DerivationRunExistsError,
        load_derivation,
        persist_snapshot,
    )
    from xraymyrepo.persistence import derivation_queries as dq


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        reset_database(connection)
        yield connection


@pytest.fixture(scope="module")
def snapshot_id(conn: Any, golden: SnapshotDocument) -> int:
    return persist_snapshot(conn, golden)


@pytest.fixture(scope="module")
def run_id(conn: Any, snapshot_id: int) -> int:
    return orchestration.derive_health(conn, snapshot_id)


def paged[T, K](
    fetch: Callable[[K | None, int], Sequence[T]], key: Callable[[T], K], limit: int = 2
) -> list[T]:
    items: list[T] = []
    after: K | None = None
    while True:
        page = fetch(after, limit)
        items += page
        if len(page) < limit:
            return items
        after = key(page[-1])


def test_health_run_round_trips(conn: Any, run_id: int, golden: SnapshotDocument) -> None:
    assert load_derivation(conn, run_id) == health(golden)
    record = dq.get_run(conn, run_id)
    assert record is not None
    assert (record.algorithm, record.algorithm_version) == ("health", "1.0.0")
    assert dq.run_params(conn, run_id) == HealthParams().as_json()
    assert dq.run_lenses(conn, run_id) == []  # health runs have no lenses


def test_health_and_architecture_runs_coexist(
    conn: Any, snapshot_id: int, run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    architecture_run = orchestration.derive_architecture(conn, snapshot_id)
    runs = {r.algorithm: r.id for r in dq.list_runs(conn, snapshot_id, limit=10)}
    assert runs == {"health": run_id, "architecture": architecture_run}

    def must_not_run(*args: Any) -> None:
        raise AssertionError("health() called for an existing run")

    monkeypatch.setattr(orchestration, "health", must_not_run)
    with pytest.raises(DerivationRunExistsError) as raised:
        orchestration.derive_health(conn, snapshot_id)
    assert raised.value.run_id == run_id


def test_metric_ranking(conn: Any, run_id: int, golden: SnapshotDocument) -> None:
    expected = sorted(
        ((m.value, m.node_key) for m in health(golden).metrics if m.metric == "fan_in"),
        key=lambda vk: (-vk[0], vk[1]),
    )
    descending = paged(
        lambda a, n: dq.list_metrics(conn, run_id, "fan_in", after=a, limit=n), dq.metric_sort_key
    )
    assert [(m.value, m.subject_key) for m in descending] == expected
    assert descending[0].subject_key == "backend/app/users/service.py"  # fan-in 2
    ascending = paged(
        lambda a, n: dq.list_metrics(conn, run_id, "fan_in", descending=False, after=a, limit=n),
        dq.metric_sort_key,
    )
    assert [(m.value, m.subject_key) for m in ascending] == sorted(expected)
    assert all(m.producer == "health" and m.rule == "health.metrics" for m in descending)
    assert dq.list_metrics(conn, run_id, "no_such_metric", limit=10) == []


def test_findings_by_subject(conn: Any, run_id: int) -> None:
    plugins = dq.list_findings(conn, run_id, subject="backend/app/plugins.py", limit=10)
    assert sorted(f.rule for _, f in plugins) == [
        "health.coverage.blind_spot",
        "health.module.unreferenced",
    ]
    blind = dq.list_findings(
        conn, run_id, rule="health.coverage.blind_spot", subject="backend/app/plugins.py", limit=10
    )
    assert [f.rule for _, f in blind] == ["health.coverage.blind_spot"]
    assert dq.list_findings(conn, run_id, subject="backend/app/db.py", limit=10) == []
