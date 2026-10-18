"""Change-risk API (``/api/v1/change-risks/...``) against PostgreSQL.

Skipped unless ``XRAY_TEST_DATABASE_URL`` points at an empty, disposable database (the
schema is dropped and recreated). Runs are stored once through
``orchestration.assess_change_risk``; every response is compared with the in-memory
``ChangeRiskAssessment`` that ``change_risk`` returns for the same documents.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xraymyrepo.analyzer import analyze
from xraymyrepo.api import Settings, create_app, cursors
from xraymyrepo.cim import (
    ChangeRiskAssessment,
    RepositoryRef,
    RiskItem,
    SnapshotDocument,
)
from xraymyrepo.insights import change_risk as algorithm
from xraymyrepo.insights.change_risk import ChangeRiskParams, change_risk
from xraymyrepo.insights.comparison import compare

from ..analyzer.helpers import write_repo
from ..conftest import ROOT, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo.orchestration import assess_change_risk, compare_snapshots
    from xraymyrepo.persistence import persist_snapshot

API = "/api/v1"
FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
OTHER = ChangeRiskParams(medium_at=1, high_at=6)


def with_identity(doc: SnapshotDocument, **identity: Any) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    return SnapshotDocument.model_validate(data)


CALC = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


@pytest.fixture(scope="module")
def docs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SnapshotDocument]:
    before = analyze(FIXTURE / "before", repository=REPO, commit_sha="1" * 40)
    after = analyze(FIXTURE / "after", repository=REPO, commit_sha="2" * 40)
    # A production file and its test both change signature: the test file is capped.
    root = tmp_path_factory.mktemp("capped")
    calc = RepositoryRef(host="local", owner="tests", name="capped")
    files: dict[str, str | bytes] = {"pkg/__init__.py": "", "pkg/calc.py": CALC,
                                     "tests/test_calc.py": CALC_TEST}  # fmt: skip
    changed: dict[str, str | bytes] = {**files, "pkg/calc.py": CALC.replace("a, b", "a, b, c=0"),
               "tests/test_calc.py": CALC_TEST.replace("test_add()", "test_add(x=1)")}  # fmt: skip
    return {
        "before": before,
        "after": after,
        "other_version": with_identity(after, commit_sha="3" * 40, extractor_version="0.3.0"),
        "calc_before": analyze(write_repo(root / "a", files), repository=calc,
                               commit_sha="a" * 40),
        "calc_after": analyze(write_repo(root / "b", changed), repository=calc,
                              commit_sha="b" * 40),
    }  # fmt: skip


@pytest.fixture(scope="module")
def stored(docs: dict[str, SnapshotDocument]) -> dict[str, int]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        reset_database(conn)
        ids = {name: persist_snapshot(conn, doc) for name, doc in docs.items()}
        ids["forward"] = compare_snapshots(conn, ids["before"], ids["after"])
        ids["backward"] = compare_snapshots(conn, ids["after"], ids["before"])  # no runs
        ids["incomparable"] = compare_snapshots(conn, ids["before"], ids["other_version"])
        ids["pending_comparison"] = conn.execute(
            "INSERT INTO snapshot_comparison (from_snapshot_id, to_snapshot_id, algorithm, "
            "algorithm_version, params_hash, params) VALUES (%s, %s, 'comparison', '9.9.9', "
            "%s, '{}') RETURNING id",
            (ids["before"], ids["after"], "sha256:" + "e" * 64),
        ).fetchone()[0]
        ids["run"] = assess_change_risk(conn, ids["forward"])
        ids["other_run"] = assess_change_risk(conn, ids["forward"], OTHER)
        ids["incomparable_run"] = assess_change_risk(conn, ids["incomparable"])
        ids["calc"] = compare_snapshots(conn, ids["calc_before"], ids["calc_after"])
        ids["capped_run"] = assess_change_risk(conn, ids["calc"])
        for name, params_hash in (("pending", "c"), ("failed", "d")):
            ids[name] = conn.execute(
                "INSERT INTO change_risk_run (comparison_id, algorithm, algorithm_version, "
                "params_hash, params, medium_at, high_at) VALUES (%s, %s, %s, %s, '{}', 2, 4) "
                "RETURNING id",
                (ids["forward"], algorithm.ALGORITHM, algorithm.ALGORITHM_VERSION,
                 "sha256:" + params_hash * 64),
            ).fetchone()[0]  # fmt: skip
        conn.execute(
            "UPDATE change_risk_run SET status = 'failed', failure_reason = 'boom', "
            "finished_at = now() WHERE id = %s",
            (ids["failed"],),
        )
    return ids


@pytest.fixture(scope="module")
def client(stored: dict[str, int]) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    with TestClient(create_app(Settings(database_url=DATABASE_URL))) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def expected(docs: dict[str, SnapshotDocument]) -> dict[str, ChangeRiskAssessment]:
    def assess(a: str, b: str, params: ChangeRiskParams | None = None) -> ChangeRiskAssessment:
        return change_risk(compare(docs[a], docs[b]), docs[a], docs[b], params)

    return {
        "run": assess("before", "after"),
        "other_run": assess("before", "after", OTHER),
        "incomparable_run": assess("before", "other_version"),
        "capped_run": assess("calc_before", "calc_after"),
    }


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def problem(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert (body["status"], body["code"]) == (status, code)
    return body


def all_pages(client: TestClient, path: str, **params: Any) -> list[Any]:
    items: list[Any] = []
    while True:
        page = ok(client.get(path, params=params))
        items += page["items"]
        if page["next_cursor"] is None:
            return items
        params = {**params, "cursor": page["next_cursor"]}


def run_url(stored: dict[str, int], name: str, tail: str = "") -> str:
    return f"{API}/change-risks/{stored[name]}{tail}"


def summary_view(i: RiskItem) -> dict[str, Any]:
    """An item as the list serves it, written out from the CIM record."""
    return {
        "subject_key": i.subject_key, "change": i.change.value, "level": i.level.value,
        "points": i.points, "confidence": i.confidence.value, "capped": i.capped,
        "conclusive": i.conclusive,
        "caveats": [c.value for c in i.caveats],
        "factors": [{"rule": f.rule.value, "points": f.points, "value": f.value,
                     "confidence": f.confidence.value, "message": f.message,
                     "caveats": [c.value for c in f.caveats]} for f in i.factors],
    }  # fmt: skip


def detail_view(i: RiskItem) -> dict[str, Any]:
    view = summary_view(i)
    for factor, record in zip(view["factors"], i.factors, strict=True):
        factor["evidence"] = [e.model_dump(mode="json") for e in record.evidence]
    return view


def riskiest_first(a: ChangeRiskAssessment) -> list[RiskItem]:
    return sorted(a.items, key=lambda i: (-i.level.rank, -i.points, i.subject_key))


# ---------------------------------------------------------------------------
# Discovery and run detail


def test_runs_of_a_comparison(client: TestClient, stored: dict[str, int]) -> None:
    """Newest first, any status, paged."""
    listed = all_pages(client, f"{API}/comparisons/{stored['forward']}/change-risks", limit=2)
    assert [r["id"] for r in listed] == sorted(
        [stored[n] for n in ("run", "other_run", "pending", "failed")], reverse=True
    )
    by_id = {r["id"]: r for r in listed}
    assert by_id[stored["pending"]]["status"] == "pending"
    assert (
        by_id[stored["pending"]]["level"] is None and by_id[stored["pending"]]["conclusive"] is None
    )
    assert by_id[stored["failed"]]["failure_reason"] == "boom"
    assert by_id[stored["run"]]["level"] == "high" and by_id[stored["run"]]["conclusive"] is False
    assert ok(client.get(f"{API}/comparisons/{stored['backward']}/change-risks"))["items"] == []


def test_discovery_errors(client: TestClient, stored: dict[str, int]) -> None:
    problem(client.get(f"{API}/comparisons/999999/change-risks"), 404, "comparison_not_found")
    problem(client.get(f"{API}/comparisons/{stored['pending_comparison']}/change-risks"), 409,
            "comparison_not_complete")  # fmt: skip
    problem(client.get(f"{API}/comparisons/0/change-risks"), 422, "validation_error")
    runs = f"{API}/comparisons/{stored['forward']}/change-risks"
    for tampered in (2**63, -1):  # outside a bigint id: rejected, never a 500
        problem(
            client.get(runs, params={"cursor": cursors.encode(tampered)}), 400, "invalid_cursor"
        )


@pytest.mark.parametrize("name", ["run", "other_run", "incomparable_run"])
def test_run_detail(
    client: TestClient, stored: dict[str, int], expected: dict[str, ChangeRiskAssessment], name: str
) -> None:
    a = expected[name]
    body = ok(client.get(run_url(stored, name)))
    run = a.run
    assert (body["id"], body["algorithm"], body["algorithm_version"], body["params_hash"],
            body["medium_at"], body["high_at"]) == (
        stored[name], run.algorithm, run.algorithm_version, run.params_hash, run.medium_at,
        run.high_at)  # fmt: skip
    assert (body["level"], body["confidence"], body["conclusive"]) == (
        a.level.value, a.confidence.value, a.conclusive)  # fmt: skip
    assert body["caveats"] == [c.value for c in a.caveats]
    assert body["excluded_by_origin"] == a.excluded_by_origin
    assert body["counts"] == a.summary().model_dump(mode="json")
    assert body["status"] == "complete" and body["finished_at"] is not None
    params = OTHER if name == "other_run" else ChangeRiskParams()
    assert body["params"] == params.as_json()
    comparison = body["comparison"]
    assert comparison["id"] == body["comparison_id"]
    assert comparison["incomparable"] == [r.value for r in run.comparison.incomparable]


def test_an_incomparable_run_says_so(client: TestClient, stored: dict[str, int]) -> None:
    body = ok(client.get(run_url(stored, "incomparable_run")))
    assert body["caveats"] == ["incomparable_snapshots"] and body["conclusive"] is False
    assert body["confidence"] != "high"


# ---------------------------------------------------------------------------
# Items


@pytest.mark.parametrize("name", ["run", "other_run", "incomparable_run"])
def test_items_riskiest_first_and_paged(
    client: TestClient, stored: dict[str, int], expected: dict[str, ChangeRiskAssessment], name: str
) -> None:
    items = all_pages(client, run_url(stored, name, "/items"), limit=3)
    assert items == [summary_view(i) for i in riskiest_first(expected[name])]
    assert all("evidence" not in f for i in items for f in i["factors"])


@pytest.mark.parametrize(
    "params",
    [
        {"level": "high"},
        {"level": ["medium", "low"]},
        {"confidence": "low"},
        {"caveat": "undetermined_changes"},
        {"caveat": ["incomplete_extraction", "undetermined_changes"]},
        {"level": "low", "confidence": "medium", "caveat": "hidden_dependencies"},
        {"level": "high", "confidence": "medium"},  # matches nothing: an empty page
    ],
)
def test_item_filters(
    client: TestClient,
    stored: dict[str, int],
    expected: dict[str, ChangeRiskAssessment],
    params: dict[str, Any],
) -> None:
    def wanted(values: Any) -> set[str] | None:
        return None if values is None else set([values] if isinstance(values, str) else values)

    levels, confidences, caveats = (
        wanted(params.get(k)) for k in ("level", "confidence", "caveat")
    )
    items = all_pages(client, run_url(stored, "run", "/items"), **params, limit=2)
    assert items == [
        summary_view(i) for i in riskiest_first(expected["run"])
        if (levels is None or i.level.value in levels)
        and (confidences is None or i.confidence.value in confidences)
        and (caveats is None or {c.value for c in i.caveats} & caveats)
    ]  # fmt: skip


def test_low_is_never_shown_as_safe(client: TestClient, stored: dict[str, int]) -> None:
    """report.py is low with low confidence: served as not conclusive, with its caveats."""
    item = ok(client.get(run_url(stored, "run", "/items/by-key"), params={"key": "shop/report.py"}))
    assert (item["level"], item["confidence"], item["conclusive"]) == ("low", "low", False)
    assert item["caveats"] == ["undetermined_changes", "incomplete_extraction",
                               "hidden_dependencies"]  # fmt: skip
    absent = next(f for f in item["factors"] if f["rule"] == "risk.test.no_dependent_test")
    assert (absent["points"], absent["caveats"]) == (0, ["hidden_dependencies"])


def test_every_item_by_key(
    client: TestClient, stored: dict[str, int], expected: dict[str, ChangeRiskAssessment]
) -> None:
    for i in expected["run"].items:
        body = ok(
            client.get(run_url(stored, "run", "/items/by-key"), params={"key": i.subject_key})
        )
        assert body == detail_view(i)
    legacy = ok(
        client.get(run_url(stored, "run", "/items/by-key"), params={"key": "shop/legacy.py"})
    )
    dependents = next(f for f in legacy["factors"] if f["rule"] == "risk.impact.dependents")
    assert [e["side"] for e in dependents["evidence"]] == ["from", "from"]
    assert dependents["evidence"][0]["edge_refs"][0]["ref"] == "edge"


def test_a_capped_test_file_is_never_conclusive(
    client: TestClient, stored: dict[str, int], expected: dict[str, ChangeRiskAssessment]
) -> None:
    """Its low is a policy (risk.cap.test_file), not what its points say: served as
    capped and not conclusive, with its uncapped points; the production file is not
    capped; the run, holding a capped item, is not conclusive either."""
    url = run_url(stored, "capped_run", "/items/by-key")
    test = ok(client.get(url, params={"key": "tests/test_calc.py"}))
    shown = (test["level"], test["points"], test["confidence"], test["caveats"])
    assert shown == ("low", 3, "high", [])
    assert (test["capped"], test["conclusive"]) == (True, False)
    assert "risk.cap.test_file" in {f["rule"] for f in test["factors"]}
    production = ok(client.get(url, params={"key": "pkg/calc.py"}))
    # Not capped: its level follows its points (kind 1 + interface 2 = 3: medium).
    assert (production["capped"], production["points"], production["level"]) == (False, 3, "medium")
    run = ok(client.get(run_url(stored, "capped_run")))
    assert (run["confidence"], run["caveats"], run["conclusive"]) == ("high", [], False)
    listed = ok(client.get(f"{API}/comparisons/{stored['calc']}/change-risks"))["items"]
    assert [(r["id"], r["conclusive"]) for r in listed] == [(stored["capped_run"], False)]
    a = expected["capped_run"]
    assert all_pages(client, run_url(stored, "capped_run", "/items"), limit=1) == [
        summary_view(i) for i in riskiest_first(a)]  # fmt: skip
    assert not a.conclusive  # the CIM view agrees


# ---------------------------------------------------------------------------
# Errors


@pytest.mark.parametrize("tail", ["", "/items", "/items/by-key?key=shop/legacy.py"])
def test_missing_and_incomplete_runs(client: TestClient, stored: dict[str, int], tail: str) -> None:
    problem(client.get(f"{API}/change-risks/999999{tail}"), 404, "change_risk_not_found")
    for name in ("pending", "failed"):
        body = problem(client.get(run_url(stored, name, tail)), 409, "change_risk_not_complete")
        assert name in body["detail"]


@pytest.mark.parametrize("run_id", ["0", "-1", "abc", "1.5"])
def test_malformed_run_ids(client: TestClient, run_id: str) -> None:
    for tail in ("", "/items", "/items/by-key?key=a.py"):
        problem(client.get(f"{API}/change-risks/{run_id}{tail}"), 422, "validation_error")


@pytest.mark.parametrize(
    "params",
    [{"level": "safe"}, {"level": "unknown"}, {"confidence": "certain"}, {"caveat": "none"},
     {"limit": 0}, {"limit": 1001}, {"limit": "x"}],
)  # fmt: skip
def test_invalid_item_parameters(
    client: TestClient, stored: dict[str, int], params: dict[str, Any]
) -> None:
    body = problem(client.get(run_url(stored, "run", "/items"), params=params), 422,
                   "validation_error")  # fmt: skip
    assert body["errors"]


def test_item_keys(client: TestClient, stored: dict[str, int]) -> None:
    url = run_url(stored, "run", "/items/by-key")
    body = problem(client.get(url, params={"key": "shop/nowhere.py"}), 404, "risk_item_not_found")
    assert "shop/nowhere.py" in body["detail"]
    # Unchanged files are not items either.
    problem(client.get(url, params={"key": "shop/__init__.py"}), 404, "risk_item_not_found")
    for key in ("shop/pricing.py#Cart", "/shop/x.py", "a//b.py", "ext:pypi:x"):
        problem(client.get(url, params={"key": key}), 422, "invalid_key")  # subjects are files
    problem(client.get(url), 422, "validation_error")  # key is required


@pytest.mark.parametrize(
    ("tail", "cursor"),
    [
        ("/items", "%%%"),
        ("/items", cursors.encode(3)),  # a run-listing cursor
        ("/items", cursors.encode([2, "4", "a.py"])),
        # Tampered values outside what the SQL compares with: rejected, never a 500.
        ("/items", cursors.encode([3, 1, "a.py"])),  # no such level rank
        ("/items", cursors.encode([10**30, 1, "a.py"])),
        ("/items", cursors.encode([1, 2**31, "a.py"])),  # beyond an SQL integer
        ("/items", cursors.encode([1, -1, "a.py"])),
        ("/items", cursors.encode([1, 1, "a\x00b.py"])),  # PostgreSQL text holds no NUL
    ],
)
def test_invalid_cursors(
    client: TestClient, stored: dict[str, int], tail: str, cursor: str
) -> None:
    problem(client.get(run_url(stored, "run", tail), params={"cursor": cursor}), 400,
            "invalid_cursor")  # fmt: skip
    # An item cursor on the run listing.
    runs = f"{API}/comparisons/{stored['forward']}/change-risks"
    item_cursor = cursors.encode([2, 4, "a.py"])
    problem(client.get(runs, params={"cursor": item_cursor}), 400, "invalid_cursor")


def test_the_api_is_read_only(client: TestClient, stored: dict[str, int]) -> None:
    for method in ("post", "put", "patch", "delete"):
        problem(getattr(client, method)(run_url(stored, "run")), 405, "method_not_allowed")
        problem(getattr(client, method)(f"{API}/comparisons/{stored['forward']}/change-risks"),
                405, "method_not_allowed")  # fmt: skip


def test_the_contract_never_offers_a_safe_level(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert schemas["RiskLevel"]["enum"] == ["low", "medium", "high"]
    for name in ("RiskItemSummary", "RiskItemDetail", "ChangeRiskRunSummary"):
        assert "not evidence" in schemas[name]["properties"]["conclusive"]["description"]


# ---------------------------------------------------------------------------
# Query budget


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
    ("path", "params", "budget", "paged"),
    [
        ("/comparisons/{forward}/change-risks", {}, 2, True),
        ("{run}", {}, 4, False),
        ("{run}/items", {}, 3, True),
        ("{run}/items", {"level": "high", "caveat": "hidden_dependencies"}, 2, True),  # empty
        ("{run}/items/by-key", {"key": "shop/legacy.py"}, 3, False),
    ],
)
def test_query_budget(
    client: TestClient,
    stored: dict[str, int],
    statements: list[str],
    path: str,
    params: dict[str, Any],
    budget: int,
    paged: bool,
) -> None:
    """A fixed number of statements per request, whatever the page size (no N+1)."""
    target = API + path.format(forward=stored["forward"], run=f"/change-risks/{stored['run']}")
    for limit in (1, 1000) if paged else (None,):
        statements.clear()
        ok(client.get(target, params={**params, "limit": limit} if limit else params))
        assert len(statements) == budget, statements
