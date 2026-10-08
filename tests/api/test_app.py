"""API behaviour that needs no database: cursors, settings, errors, CORS, OpenAPI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from xraymyrepo.api import Settings, create_app, cursors
from xraymyrepo.api.errors import ApiError

from ..conftest import ROOT

OPENAPI = ROOT / "docs" / "api" / "openapi-v1.json"
# Nothing listens on port 1: every connection attempt fails at once.
UNREACHABLE = Settings(database_url="postgresql://xray:x@127.0.0.1:1/none", pool_timeout=0.5)


def test_cursor_round_trip() -> None:
    shape: TypeAdapter[tuple[int, int, int, str]] = TypeAdapter(tuple[int, int, int, str])
    key = (1, 12, 4, "src/a b#c.py:%")
    assert cursors.decode(cursors.encode(key), shape) == key
    assert cursors.decode(None, shape) is None


@pytest.mark.parametrize(
    "cursor", ["%%%", "bm90IGpzb24", cursors.encode([1, "x"]), cursors.encode(["1", 2, 3, "k"])]
)
def test_bad_cursors_are_rejected(cursor: str) -> None:
    shape: TypeAdapter[tuple[int, int, int, str]] = TypeAdapter(tuple[int, int, int, str])
    with pytest.raises(ApiError) as raised:
        cursors.decode(cursor, shape)
    assert (raised.value.status, raised.value.code) == (400, "invalid_cursor")


def test_page_splits_the_extra_row() -> None:
    assert cursors.page([1, 2, 3], 3, str) == ([1, 2, 3], None)
    items, next_cursor = cursors.page([1, 2, 3, 4], 3, lambda i: i)
    assert items == [1, 2, 3]
    assert next_cursor is not None
    assert cursors.decode(next_cursor, TypeAdapter(int)) == 3


def test_settings_from_env() -> None:
    settings = Settings.from_env({"XRAY_DATABASE_URL": "postgresql://h/db",
                                  "XRAY_CORS_ORIGINS": "http://a, http://b ,",
                                  "XRAY_DB_POOL_MAX": "3"})  # fmt: skip
    assert settings == Settings(database_url="postgresql://h/db", pool_max_size=3,
                                cors_origins=("http://a", "http://b"))  # fmt: skip
    with pytest.raises(RuntimeError, match="XRAY_DATABASE_URL"):
        Settings.from_env({})


def test_health_needs_no_database() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_unreachable_database_is_503() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "unavailable"
        response = client.get("/api/v1/snapshots/1")
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/problem+json"
        body = response.json()
        assert body["code"] == "database_unavailable"
        assert "127.0.0.1" not in json.dumps(body)  # connection details are not disclosed


def test_unknown_routes_and_methods_are_problems() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        missing = client.get("/api/v1/nope")
        assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
        assert missing.headers["content-type"] == "application/problem+json"
        assert client.post("/health").json()["code"] == "method_not_allowed"


def test_cors_is_off_by_default_and_read_only_when_configured() -> None:
    preflight = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"}
    with TestClient(create_app(UNREACHABLE)) as client:
        assert "access-control-allow-origin" not in client.get("/health", headers=preflight).headers
    settings = Settings(database_url=UNREACHABLE.database_url, pool_timeout=0.5,
                        cors_origins=("http://localhost:5173",))  # fmt: skip
    with TestClient(create_app(settings)) as client:
        allowed = client.options("/api/v1/repositories", headers=preflight)
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
        post = {**preflight, "Access-Control-Request-Method": "POST"}
        assert client.options("/api/v1/repositories", headers=post).status_code == 400


def test_openapi_contract_is_unchanged() -> None:
    """The committed contract (docs/api/openapi-v1.json) is the public API. Regenerate it
    deliberately with XRAY_UPDATE_OPENAPI=1 and review the diff."""
    spec = json.dumps(create_app(UNREACHABLE).openapi(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("XRAY_UPDATE_OPENAPI"):
        OPENAPI.parent.mkdir(parents=True, exist_ok=True)
        OPENAPI.write_text(spec, encoding="utf-8", newline="\n")
    assert Path(OPENAPI).read_text(encoding="utf-8") == spec


def test_openapi_documents_problems_and_no_write_methods() -> None:
    spec = create_app(UNREACHABLE).openapi()
    for path, operations in spec["paths"].items():
        assert set(operations) == {"get"}, path
        if path.startswith("/api/v1/"):
            documented = operations["get"]["responses"]
            assert "application/problem+json" in documented["422"]["content"], path
            assert "503" in documented, path
