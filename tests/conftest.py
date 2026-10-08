from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.cim import SnapshotDocument

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "fixtures" / "golden" / "v1"
GOLDEN_REPO = GOLDEN_DIR / "repo"
GOLDEN_JSON = GOLDEN_DIR / "expected_cim.json"
MIGRATIONS = ROOT / "db" / "migrations"

PRODUCER_HASH = "sha256:" + "0" * 64
SHA = "0123456789abcdef0123456789abcdef01234567"


def reset_database(connection: Any) -> None:
    """Drop everything in the disposable test database and apply every migration."""
    connection.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        connection.execute(migration.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def golden_raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def golden_data(golden_raw: dict[str, Any]) -> dict[str, Any]:
    """A fresh, mutable copy of the golden document for negative tests."""
    return copy.deepcopy(golden_raw)


@pytest.fixture(scope="session")
def golden(golden_raw: dict[str, Any]) -> SnapshotDocument:
    return SnapshotDocument.model_validate(golden_raw)


def prov(producer: str = "python-extractor", rule: str | None = "py.test.rule") -> dict[str, Any]:
    return {"producer": producer, "rule": rule}


def span_ev(
    basis: str = "resolved",
    file_key: str = "src/a.py",
    line: int = 1,
    rule: str | None = "py.test.rule",
) -> dict[str, Any]:
    return {
        "type": "span",
        "basis": basis,
        "provenance": prov(rule=rule),
        "file_key": file_key,
        "start_line": line,
        "start_col": 0,
        "end_line": line,
        "end_col": 10,
        "ast_type": "import_statement",
    }
