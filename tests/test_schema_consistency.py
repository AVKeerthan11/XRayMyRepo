"""The SQL CHECK constraints and the Python contract enumerate the same vocabularies."""

from __future__ import annotations

import itertools
import json
import re

import pytest

from xraymyrepo.cim import (
    EDGE_RULES,
    ClassificationFacet,
    Confidence,
    EdgeChangeKind,
    EdgeKind,
    ExtractionStatus,
    FindingSeverity,
    Incomparability,
    IssueSeverity,
    LensKind,
    MatchMethod,
    NodeChangeKind,
    Origin,
    ReferenceKind,
    Role,
    SnapshotStatus,
    UnresolvedReason,
)
from xraymyrepo.cim.enums import DERIVATION_BASES, SNAPSHOT_BASES, SNAPSHOT_NODE_KINDS

from .conftest import MIGRATIONS

SQL = (MIGRATIONS / "0001_cim_v1_snapshot_tier.sql").read_text(encoding="utf-8")
DERIVATION_SQL = (MIGRATIONS / "0003_derivation_tier.sql").read_text(encoding="utf-8")
COMPARISON_SQL = (MIGRATIONS / "0004_snapshot_comparison.sql").read_text(encoding="utf-8")


def check_values(constraint: str, sql: str = SQL) -> set[str]:
    # The constraint body runs until the next table element or the end of the table.
    match = re.search(rf"CONSTRAINT {constraint} CHECK (.*?)(?=\n\s*(?:CONSTRAINT|\);))", sql, re.S)
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


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("derivation_run_status_check", values(SnapshotStatus)),
        ("lens_kind_check", values(LensKind)),
        ("group_edge_kind_check", values(EdgeKind)),
        ("finding_severity_check", values(FindingSeverity)),
        *[(f"{table}_basis_check", {b.value for b in DERIVATION_BASES})
          for table in ("derived_group", "group_member", "finding")],
        *[(f"{table}_confidence_check", values(Confidence))
          for table in ("derived_group", "group_member", "finding", "inferred_layer")],
    ],
)  # fmt: skip
def test_derivation_check_constraint_matches_enum(constraint: str, expected: set[str]) -> None:
    assert check_values(constraint, DERIVATION_SQL) == expected


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("snapshot_comparison_status_check", values(SnapshotStatus)),
        ("node_change_change_check", values(NodeChangeKind)),
        ("node_change_kind_check", {k.value for k in SNAPSHOT_NODE_KINDS}),
        ("node_change_match_method_check", values(MatchMethod)),
        ("node_change_confidence_check", values(Confidence)),
        # unchanged edges are counted in the summary, never rows
        ("edge_change_change_check", values(EdgeChangeKind) - {EdgeChangeKind.UNCHANGED.value}),
        ("edge_change_kind_check", values(EdgeKind)),
        ("edge_change_confidence_check", values(Confidence)),
    ],
)
def test_comparison_check_constraint_matches_enum(constraint: str, expected: set[str]) -> None:
    assert check_values(constraint, COMPARISON_SQL) == expected


def test_comparison_incomparable_is_every_ordered_subset_of_the_reasons() -> None:
    body = re.search(r"CONSTRAINT snapshot_comparison_incomparable_check CHECK (.*?)\),\n",
                     COMPARISON_SQL, re.S)  # fmt: skip
    assert body
    allowed = {tuple(json.loads(v)) for v in re.findall(r"'(\[[^']*\])'", body.group(1))}
    reasons = [r.value for r in Incomparability]
    expected = {tuple(r for r in reasons if r in subset)
                for n in range(len(reasons) + 1)
                for subset in itertools.combinations(reasons, n)}  # fmt: skip
    assert allowed == expected


def test_comparison_summary_lists_every_change_kind() -> None:
    body = re.search(r"FUNCTION snapshot_comparison_summary_valid\(.*?\$\$(.*?)\$\$;",
                     COMPARISON_SQL, re.S)  # fmt: skip
    assert body
    assert "summary IS NULL OR snapshot_comparison_summary_valid(summary)" in COMPARISON_SQL
    arrays = re.findall(r"summary -> '(nodes|edges)' \?& ARRAY\[([^\]]*)\]", body.group(1))
    keys = {side: set(re.findall(r"'([^']+)'", items)) for side, items in arrays}
    assert keys == {"nodes": values(NodeChangeKind), "edges": values(EdgeChangeKind)}
    removed = re.findall(r"\(summary -> '(nodes|edges)'\) - ARRAY\[([^\]]*)\]", body.group(1))
    assert {side: set(re.findall(r"'([^']+)'", items)) for side, items in removed} == keys


def test_comparison_tier_shape() -> None:
    assert "CREATE TYPE" not in COMPARISON_SQL.upper() and "USING gin" not in COMPARISON_SQL
    for table in ("node_change", "edge_change"):
        body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", COMPARISON_SQL, re.S)
        assert body, table
        assert re.search(r"PRIMARY KEY \(comparison_id,", body.group(1)), table
        # Snapshot rows are referenced through the comparison's own snapshots, deferred.
        reference = (
            r"FOREIGN KEY \((\w+), (\w+)\)\s+REFERENCES (node|edge) \(snapshot_id, id\)"
            r" DEFERRABLE INITIALLY DEFERRED"
        )
        refs = re.findall(reference, body.group(1))
        assert {r[0] for r in refs} == {"from_snapshot_id", "to_snapshot_id"}, table
        assert "ON DELETE CASCADE" in body.group(1), table


def test_derivation_tier_shape() -> None:
    assert "CONSTRAINT inferred_layer_basis_check CHECK (basis = 'inferred')" in DERIVATION_SQL
    rule_required = r"CREATE TABLE inferred_layer \(.*?\n    rule\s+text NOT NULL,"
    assert re.search(rule_required, DERIVATION_SQL, re.S)
    assert "CREATE TYPE" not in DERIVATION_SQL.upper() and "USING gin" not in DERIVATION_SQL
    for table in ("derived_group", "group_member", "group_edge", "finding", "inferred_layer"):
        body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", DERIVATION_SQL, re.S)
        assert body, table
        assert re.search(r"PRIMARY KEY \(run_id,", body.group(1)), table
