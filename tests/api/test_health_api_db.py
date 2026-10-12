"""Health through the API: the findings ``subject`` filter, the metrics ranking and the
run detail of a health run. Skipped unless ``XRAY_TEST_DATABASE_URL`` is set."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.api import Settings, create_app
from xraymyrepo.cim import SnapshotDocument
from xraymyrepo.insights.health import HealthParams, health

from ..conftest import reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import derive_architecture, derive_health
    from xraymyrepo.persistence import persist_snapshot

API = "/api/v1"


@pytest.fixture(scope="module")
def stored(golden: SnapshotDocument) -> dict[str, int]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        snapshot = persist_snapshot(conn, golden)
        return {
            "snapshot": snapshot,
            "health": derive_health(conn, snapshot),
            "architecture": derive_architecture(conn, snapshot),
        }


@pytest.fixture(scope="module")
def client(stored: dict[str, int]) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def run(stored: dict[str, int]) -> str:
    return f"{API}/derivations/{stored['health']}"


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code


def all_pages(client: TestClient, path: str, **params: Any) -> list[Any]:
    items: list[Any] = []
    while True:
        page = ok(client.get(path, params=params))
        items += page["items"]
        if page["next_cursor"] is None:
            return items
        params["cursor"] = page["next_cursor"]


def test_health_run_detail(client: TestClient, run: str) -> None:
    detail = ok(client.get(run))
    assert (detail["algorithm"], detail["params"]) == ("health", HealthParams().as_json())
    assert detail["finding_counts"] == {
        "health.coverage.blind_spot": 2,
        "health.module.unreferenced": 1,
    }
    # Architecture-specific parts are empty for a health run (documented, not omitted).
    assert detail["lenses"] == []
    assert detail["layer_coverage"]["considered"] == 0


def test_findings_distinguish_observed_from_inferred(
    client: TestClient, run: str, golden: SnapshotDocument
) -> None:
    listed = all_pages(client, f"{run}/findings", limit=1)
    assert [{k: v for k, v in f.items() if k != "id"} for f in listed] == [
        f.model_dump(mode="json") for f in health(golden).findings
    ]
    basis = {f["rule"]: f["basis"] for f in listed}
    assert basis == {
        "health.coverage.blind_spot": "observed",
        "health.module.unreferenced": "inferred",
    }
    blind = next(
        f
        for f in listed
        if f["rule"] == "health.coverage.blind_spot"
        and f["subject_keys"] == ["backend/app/plugins.py"]
    )
    assert {e["type"] for e in blind["evidence"]} == {"span"}


def test_findings_subject_filter(client: TestClient, run: str, stored: dict[str, int]) -> None:
    about = ok(client.get(f"{run}/findings", params={"subject": "backend/app/plugins.py"}))
    assert sorted(f["rule"] for f in about["items"]) == [
        "health.coverage.blind_spot",
        "health.module.unreferenced",
    ]
    # Group keys are subjects too (architecture cycle findings).
    arch = f"{API}/derivations/{stored['architecture']}/findings"
    app = "group:components:pkg:pypi:acme-backend#app"
    assert [f["rule"] for f in ok(client.get(arch, params={"subject": app}))["items"]] == [
        "arch.group.cycle"
    ]
    problem(client.get(f"{run}/findings", params={"subject": "a//b"}), 422, "invalid_key")
    problem(client.get(f"{run}/findings", params={"subject": "group:bad"}), 422, "invalid_key")


def test_metrics_ranking(
    client: TestClient, run: str, golden: SnapshotDocument, stored: dict[str, int]
) -> None:
    ranked = all_pages(client, f"{run}/metrics", metric="fan_in", limit=2)
    expected = sorted(
        ((m.node_key, m.value) for m in health(golden).metrics if m.metric == "fan_in"),
        key=lambda kv: (-kv[1], kv[0]),
    )
    assert [(m["subject_key"], m["value"]) for m in ranked] == expected
    assert ranked[0]["provenance"] == {"producer": "health", "rule": "health.metrics"}
    ascending = all_pages(client, f"{run}/metrics", metric="fan_out", order="asc", limit=3)
    assert [m["value"] for m in ascending] == sorted(m["value"] for m in ascending)
    # Group metrics of an architecture run come through the same endpoint.
    arch = ok(
        client.get(
            f"{API}/derivations/{stored['architecture']}/metrics", params={"metric": "instability"}
        )
    )
    assert all(m["subject_key"].startswith("group:") for m in arch["items"])
    problem(client.get(f"{run}/metrics"), 422, "validation_error")
    problem(
        client.get(f"{run}/metrics", params={"metric": "fan_in", "order": "up"}),
        422,
        "validation_error",
    )
    problem(
        client.get(f"{run}/metrics", params={"metric": "fan_in", "cursor": "x"}),
        400,
        "invalid_cursor",
    )


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


@pytest.mark.parametrize(
    ("path", "params"),
    [("/metrics", {"metric": "fan_in"}), ("/findings", {"subject": "backend/app/plugins.py"})],
)
def test_query_budget(
    client: TestClient, run: str, statements: list[str], path: str, params: dict[str, Any]
) -> None:
    for limit in (1, 1000):
        statements.clear()
        ok(client.get(f"{run}{path}", params={**params, "limit": limit}))
        assert len(statements) == 2, statements  # run check + one query
