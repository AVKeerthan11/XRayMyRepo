"""The analyzer against the golden fixture: real source code -> the expected CIM.

The golden document is polyglot and describes extractors that do not exist yet
(TypeScript, HANDLES, TESTS, INHERITS, roles, REQUIRES). The analyzer's output is
compared with the golden document on the slice this analyzer is responsible for,
with exact equality in both directions; and the analyzer must emit nothing outside
that slice.

One intentional exception (approved review decision): ``ExtractionIssue.message`` is
not compared, to avoid CPython-version-specific wording. It is the
parser's human-readable text, which depends on the Python version (CPython 3.13
reports ``invalid syntax`` where the fixture spells out ``invalid syntax: expected
a parameter name``). Code, severity, file, span and provenance are compared exactly.
"""

from __future__ import annotations

from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Classification,
    ClassificationFacet,
    Edge,
    EdgeKind,
    ExtractionIssue,
    Node,
    NodeKind,
    Role,
    SnapshotDocument,
    UnresolvedReference,
)

from ..conftest import GOLDEN_REPO

# Languages the golden document extracts but this analyzer does not: their files'
# extraction_status differs by design (``full`` there, ``unsupported`` here).
NOT_EXTRACTED_HERE = frozenset({"typescript", "json"})
IN_SCOPE_ECOSYSTEMS = frozenset({"pypi", "python-stdlib"})


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                   commit_sha=golden.snapshot.commit_sha)  # fmt: skip


def _node_in_scope(node: Node) -> bool:
    if node.kind in (NodeKind.REPOSITORY, NodeKind.DIRECTORY, NodeKind.FILE):
        return True
    if node.kind in (NodeKind.TYPE, NodeKind.FUNCTION):
        return node.language == "python"
    if node.kind in (NodeKind.PACKAGE, NodeKind.EXTERNAL_PACKAGE):
        return getattr(node.attributes, "ecosystem", None) in IN_SCOPE_ECOSYSTEMS
    return False


def _comparable_node(node: Node) -> Node:
    if node.kind is NodeKind.FILE and node.language in NOT_EXTRACTED_HERE:
        return node.model_copy(update={"extraction_status": None})
    return node


def _python_source(doc: SnapshotDocument, key: str) -> bool:
    return doc.node(key).language == "python"


def _edge_in_scope(doc: SnapshotDocument, edge: Edge) -> bool:
    return edge.kind is EdgeKind.IMPORTS and _python_source(doc, edge.source_key)


def _classification_in_scope(c: Classification) -> bool:
    return c.facet is ClassificationFacet.ORIGIN or (
        c.facet is ClassificationFacet.ROLE
        and c.value == Role.MANIFEST
        and c.node_key.endswith("pyproject.toml")
    )


def _issue_in_scope(doc: SnapshotDocument, issue: ExtractionIssue) -> bool:
    return issue.file_key is not None and _python_source(doc, issue.file_key)


def _comparable_issue(issue: ExtractionIssue) -> ExtractionIssue:
    return issue.model_copy(update={"message": "-"})


def _unresolved_in_scope(doc: SnapshotDocument, ref: UnresolvedReference) -> bool:
    return _python_source(doc, ref.file_key)


def _project(doc: SnapshotDocument) -> dict[str, dict[Any, Any]]:
    return {
        "nodes": {n.key: _comparable_node(n) for n in doc.nodes if _node_in_scope(n)},
        "edges": {e.ref: e for e in doc.edges if _edge_in_scope(doc, e)},
        "classifications": {
            (c.node_key, c.facet, c.value): c
            for c in doc.classifications
            if _classification_in_scope(c)
        },
        "unresolved": {
            (u.file_key, u.start_line, u.start_col, u.raw_text): u
            for u in doc.unresolved_references
            if _unresolved_in_scope(doc, u)
        },
        "issues": {
            (i.file_key, i.code, i.span.start_line if i.span else 0): _comparable_issue(i)
            for i in doc.extraction_issues
            if _issue_in_scope(doc, i)
        },
    }


@pytest.mark.parametrize("section", ["nodes", "edges", "classifications", "unresolved", "issues"])
def test_analyzer_matches_golden_slice(
    analyzed: SnapshotDocument, golden: SnapshotDocument, section: str
) -> None:
    ours = _project(analyzed)[section]
    expected = _project(golden)[section]
    assert sorted(map(str, ours.keys() - expected.keys())) == [], "unexpected records"
    assert sorted(map(str, expected.keys() - ours.keys())) == [], "missing records"
    for key, record in expected.items():
        assert ours[key] == record, f"{section} {key!r} differs"


def test_analyzer_emits_nothing_outside_its_slice(analyzed: SnapshotDocument) -> None:
    assert all(_node_in_scope(n) for n in analyzed.nodes)
    assert all(_edge_in_scope(analyzed, e) for e in analyzed.edges)
    assert all(_classification_in_scope(c) for c in analyzed.classifications)
    assert all(_unresolved_in_scope(analyzed, u) for u in analyzed.unresolved_references)
    assert all(_issue_in_scope(analyzed, i) for i in analyzed.extraction_issues)


def test_parse_error_message_is_the_parsers_own(analyzed: SnapshotDocument) -> None:
    (issue,) = analyzed.extraction_issues
    assert issue.code == "parse_error"
    assert issue.message.startswith("invalid syntax")


def test_slice_is_not_vacuous(analyzed: SnapshotDocument) -> None:
    projected = _project(analyzed)
    assert len(projected["edges"]) == 12
    assert len(projected["unresolved"]) == 2
    assert {n.kind for n in projected["nodes"].values()} == {
        NodeKind.REPOSITORY, NodeKind.DIRECTORY, NodeKind.FILE, NodeKind.TYPE,
        NodeKind.FUNCTION, NodeKind.PACKAGE, NodeKind.EXTERNAL_PACKAGE,
    }  # fmt: skip


def test_output_is_a_valid_deterministic_snapshot(
    analyzed: SnapshotDocument, golden: SnapshotDocument
) -> None:
    text = analyzed.to_canonical_json()
    assert SnapshotDocument.model_validate_json(text).to_canonical_json() == text
    again = analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                    commit_sha=golden.snapshot.commit_sha)  # fmt: skip
    assert again.to_canonical_json() == text
    assert [(c.language, c.edge_kinds) for c in analyzed.coverage] == [
        ("python", (EdgeKind.IMPORTS,))
    ]
