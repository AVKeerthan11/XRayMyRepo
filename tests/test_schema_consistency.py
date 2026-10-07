"""The SQL CHECK constraints and the Python contract enumerate the same vocabularies."""

from __future__ import annotations

import re

import pytest

from xraymyrepo.cim import (
    EDGE_RULES,
    ClassificationFacet,
    Confidence,
    EdgeKind,
    ExtractionStatus,
    IssueSeverity,
    Origin,
    ReferenceKind,
    Role,
    SnapshotStatus,
    UnresolvedReason,
)
from xraymyrepo.cim.enums import SNAPSHOT_BASES, SNAPSHOT_NODE_KINDS

from .conftest import MIGRATIONS

SQL = (MIGRATIONS / "0001_cim_v1_snapshot_tier.sql").read_text(encoding="utf-8")


def check_values(constraint: str) -> set[str]:
    # The constraint body runs until the next table element or the end of the table.
    match = re.search(rf"CONSTRAINT {constraint} CHECK (.*?)(?=\n\s*(?:CONSTRAINT|\);))", SQL, re.S)
    assert match, f"constraint {constraint} not found"
    in_list = re.search(r"IN \((.*?)\)", match.group(1), re.S)
    assert in_list, f"constraint {constraint} has no IN list"
    return set(re.findall(r"'([^']+)'", in_list.group(1)))


def values(enum: type) -> set[str]:
    return {m.value for m in enum}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("node_kind_check", {k.value for k in SNAPSHOT_NODE_KINDS}),
        ("node_basis_check", {b.value for b in SNAPSHOT_BASES}),
        ("edge_basis_check", {b.value for b in SNAPSHOT_BASES}),
        ("classification_basis_check", {b.value for b in SNAPSHOT_BASES}),
        ("node_confidence_check", values(Confidence)),
        ("edge_confidence_check", values(Confidence)),
        ("classification_confidence_check", values(Confidence)),
        ("node_extraction_status_check", values(ExtractionStatus)),
        ("edge_kind_check", values(EdgeKind)),
        ("classification_facet_check", values(ClassificationFacet)),
        ("classification_role_check", values(Role)),
        ("classification_origin_check", values(Origin)),
        ("snapshot_status_check", values(SnapshotStatus)),
        ("unresolved_reference_ref_kind_check", values(ReferenceKind)),
        ("unresolved_reference_reason_check", values(UnresolvedReason)),
        ("extraction_issue_severity_check", values(IssueSeverity)),
    ],
)
def test_check_constraint_matches_enum(constraint: str, expected: set[str]) -> None:
    assert check_values(constraint) == expected


def test_edge_kind_basis_check_matches_edge_rules() -> None:
    match = re.search(r"CONSTRAINT edge_kind_basis_check CHECK (.*?)(?=\n\s*(?:CONSTRAINT|\);))",
                      SQL, re.S)  # fmt: skip
    assert match, "constraint edge_kind_basis_check not found"
    clauses = re.findall(r"kind = '(\w+)' AND basis IN \(([^)]*)\)", match.group(1))
    sql_rules = {kind: set(re.findall(r"'([^']+)'", bases)) for kind, bases in clauses}
    assert len(clauses) == len(sql_rules)
    assert sql_rules == {k.value: {b.value for b in r.bases} for k, r in EDGE_RULES.items()}


def test_no_postgres_enum_types_and_no_graph_database() -> None:
    assert "CREATE TYPE" not in SQL.upper()
    assert "neo4j" not in SQL.lower()


def test_snapshot_scoped_keys_lead_with_snapshot_id() -> None:
    for table in ("node", "edge", "classification", "unresolved_reference", "extraction_issue"):
        body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", SQL, re.S)
        assert body, table
        assert "PRIMARY KEY (snapshot_id, id)" in body.group(1), table
        for fk in re.findall(r"FOREIGN KEY \(([^)]*)\) REFERENCES (node|snapshot_producer)",
                             body.group(1)):  # fmt: skip
            assert fk[0].startswith("snapshot_id,"), (table, fk)


def test_evidence_is_not_gin_indexed() -> None:
    assert "USING gin" not in SQL and "USING GIN" not in SQL
