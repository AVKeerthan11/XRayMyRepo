"""Snapshot comparison models (cim/comparison.py): run identity and comparability,
node/edge change rules, cross-record checks, lineage, summary and canonical form."""

from __future__ import annotations

import random
from typing import Any

import pytest
from pydantic import ValidationError

from xraymyrepo.cim import (
    ComparisonRun,
    Confidence,
    EdgeChange,
    EdgeChangeKind,
    EdgeKind,
    EdgeRef,
    Incomparability,
    MatchMethod,
    NodeChange,
    NodeChangeKind,
    NodeKind,
    RepositoryRef,
    SnapshotComparison,
    SnapshotIdentity,
    SnapshotStatus,
    incomparability,
)

from .test_tiers import config, identity

OTHER_SHA = "b" * 40
HASH = "sha256:" + "0" * 64
FROM = identity()
TO = identity(commit=OTHER_SHA)
ADDED, REMOVED, MODIFIED = NodeChangeKind.ADDED, NodeChangeKind.REMOVED, NodeChangeKind.MODIFIED
MOVED, UNCHANGED = NodeChangeKind.MOVED, NodeChangeKind.UNCHANGED
UNDETERMINED = NodeChangeKind.UNDETERMINED
SAME_KEY, SAME_CONTENT = MatchMethod.SAME_KEY, MatchMethod.SAME_CONTENT_HASH
HIGH, MEDIUM, LOW = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW


def run(
    from_snapshot: SnapshotIdentity = FROM, to_snapshot: SnapshotIdentity = TO
) -> ComparisonRun:
    return ComparisonRun.between(
        from_snapshot,
        to_snapshot,
        algorithm="comparison",
        algorithm_version="1.0.0",
        params_hash=HASH,
        status=SnapshotStatus.COMPLETE,
    )


def node(change: NodeChangeKind, kind: NodeKind = NodeKind.FILE, **fields: Any) -> NodeChange:
    return NodeChange(change=change, kind=kind, **fields)


def same(change: NodeChangeKind, key: str, kind: NodeKind = NodeKind.FILE,
         confidence: Confidence = HIGH) -> NodeChange:  # fmt: skip
    return node(change, kind, from_key=key, to_key=key, match_method=SAME_KEY,
                confidence=confidence)  # fmt: skip


def imports(source: str, target: str) -> EdgeRef:
    return EdgeRef.of(EdgeKind.IMPORTS, source, target)


def edge(change: EdgeChangeKind, from_ref: EdgeRef | None = None, to_ref: EdgeRef | None = None,
         confidence: Confidence = HIGH) -> EdgeChange:  # fmt: skip
    return EdgeChange(change=change, kind=EdgeKind.IMPORTS, from_ref=from_ref, to_ref=to_ref,
                      confidence=confidence)  # fmt: skip


def comparison(*records: NodeChange | EdgeChange, unchanged_edges: int = 0,
               comparison_run: ComparisonRun | None = None) -> SnapshotComparison:  # fmt: skip
    return SnapshotComparison(
        run=comparison_run or run(),
        nodes=tuple(r for r in records if isinstance(r, NodeChange)),
        edges=tuple(r for r in records if isinstance(r, EdgeChange)),
        unchanged_edges=unchanged_edges,
    )


def full() -> SnapshotComparison:
    """One of everything: a modified file, an added and a removed file, a moved file
    with its class, an undetermined function, an added and a removed import."""
    return comparison(
        same(UNCHANGED, "/", NodeKind.REPOSITORY),
        same(MODIFIED, "app.py"),
        node(ADDED, to_key="new.py", confidence=HIGH),
        node(REMOVED, from_key="gone.py", confidence=HIGH, move_ambiguous=True),
        node(MOVED, from_key="old/m.py", to_key="new/m.py", match_method=SAME_CONTENT,
             confidence=MEDIUM),
        node(MOVED, NodeKind.TYPE, from_key="old/m.py#A", to_key="new/m.py#A",
             match_method=SAME_CONTENT, confidence=MEDIUM),
        same(UNDETERMINED, "app.py#f", NodeKind.FUNCTION, confidence=LOW),
        edge(EdgeChangeKind.ADDED, to_ref=imports("app.py", "new.py")),
        edge(EdgeChangeKind.REMOVED, from_ref=imports("app.py", "gone.py")),
        edge(EdgeChangeKind.MODIFIED, imports("app.py", "old/m.py"), imports("app.py", "new/m.py"),
             confidence=MEDIUM),
        unchanged_edges=3,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Run identity and comparability


class TestRun:
    def test_identical_extraction_is_comparable(self) -> None:
        r = run()
        assert r.incomparable == () and r.comparable

    def test_incomparability_lists_every_reason_in_enum_order(self) -> None:
        other = identity(commit=OTHER_SHA, version="2.0.0", cfg=config(languages=("python",)))
        assert incomparability(FROM, other) == (
            Incomparability.EXTRACTOR_VERSION_DIFFERS,
            Incomparability.CONFIG_DIFFERS,
        )
        assert incomparability(FROM, identity(version="2.0.0")) == (
            Incomparability.EXTRACTOR_VERSION_DIFFERS,
        )
        # The same commit under another extractor version is a valid, incomparable pair.
        r = run(FROM, identity(version="2.0.0"))
        assert not r.comparable

    def test_incomparable_must_match_the_identities(self) -> None:
        data = run().model_dump()
        data["incomparable"] = ["config_differs"]
        with pytest.raises(ValidationError, match="exactly how the snapshot identities differ"):
            ComparisonRun.model_validate(data)
        data = run(FROM, identity(version="2.0.0")).model_dump()
        data["incomparable"] = []
        with pytest.raises(ValidationError, match="exactly how"):
            ComparisonRun.model_validate(data)

    def test_two_different_snapshots_of_one_repository(self) -> None:
        with pytest.raises(ValidationError, match="two different snapshots"):
            run(FROM, FROM)
        elsewhere = FROM.model_copy(
            update={"repository": RepositoryRef(host="github.com", owner="acme", name="other")}
        )
        with pytest.raises(ValidationError, match="same repository"):
            run(FROM, elsewhere)


# ---------------------------------------------------------------------------
# Node changes


VALID_NODES = [
    node(ADDED, to_key="a.py", confidence=HIGH),
    node(ADDED, to_key="a.py", confidence=MEDIUM, move_ambiguous=True),
    node(REMOVED, from_key="a.py", confidence=HIGH),
    same(UNCHANGED, "a.py"),
    same(MODIFIED, "a.py#f", NodeKind.FUNCTION, MEDIUM),
    node(MOVED, from_key="a.py", to_key="b.py", match_method=SAME_CONTENT, confidence=MEDIUM),
    same(UNDETERMINED, "a.py#f", NodeKind.FUNCTION, LOW),
    node(UNDETERMINED, NodeKind.FUNCTION, from_key="a.py#f", confidence=LOW),
    node(UNDETERMINED, NodeKind.FUNCTION, to_key="a.py#f", confidence=LOW),
    same(UNCHANGED, "/", NodeKind.REPOSITORY),
]


@pytest.mark.parametrize("change", VALID_NODES, ids=lambda c: f"{c.change}-{c.key}")
def test_valid_node_changes(change: NodeChange) -> None:
    assert NodeChange.model_validate(change.model_dump()) == change


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        # key presence by change kind
        ({"change": ADDED, "from_key": "a.py", "to_key": "a.py"}, "only a to_key"),
        ({"change": ADDED, "to_key": "a.py", "match_method": SAME_KEY}, "only a to_key"),
        ({"change": REMOVED, "to_key": "a.py"}, "only a from_key"),
        ({"change": UNCHANGED, "from_key": "a.py", "to_key": "b.py", "match_method": SAME_KEY},
         "same key on both sides"),
        ({"change": MODIFIED, "from_key": "a.py", "match_method": SAME_KEY}, "same key"),
        ({"change": UNCHANGED, "from_key": "a.py", "to_key": "a.py"}, "matched by same_key"),
        ({"change": MODIFIED, "from_key": "a.py", "to_key": "a.py",
          "match_method": SAME_CONTENT}, "matched by same_key"),
        # moved
        ({"change": MOVED, "from_key": "a.py", "to_key": "a.py", "match_method": SAME_CONTENT,
          "confidence": MEDIUM}, "two different keys"),
        ({"change": MOVED, "from_key": "a.py", "to_key": "b.py", "match_method": SAME_KEY,
          "confidence": MEDIUM}, "same_content_hash"),
        ({"change": MOVED, "from_key": "a.py", "to_key": "b.py",
          "match_method": MatchMethod.GIT_RENAME, "confidence": MEDIUM}, "same_content_hash"),
        ({"change": MOVED, "from_key": "a.py", "to_key": "b.py", "match_method": SAME_CONTENT},
         "medium confidence"),
        ({"change": MOVED, "from_key": "a.py", "to_key": "b.py", "match_method": SAME_CONTENT,
          "confidence": LOW}, "medium confidence"),
        # undetermined
        ({"change": UNDETERMINED, "from_key": "a.py", "to_key": "a.py", "match_method": SAME_KEY},
         "low confidence"),
        ({"change": UNDETERMINED, "from_key": "a.py", "match_method": SAME_KEY,
          "confidence": LOW}, "no match"),
        ({"change": UNDETERMINED, "confidence": LOW}, "have a key"),
        # confidence of decided changes
        ({"change": ADDED, "to_key": "a.py", "confidence": LOW}, "high or medium"),
        # kinds and keys
        ({"change": ADDED, "kind": NodeKind.GROUP, "to_key": "group:components:/#x"},
         "not snapshot nodes"),
        ({"change": ADDED, "kind": NodeKind.FUNCTION, "to_key": "a.py"}, "symbol key"),
        ({"change": MOVED, "kind": NodeKind.FUNCTION, "from_key": "a.py#f", "to_key": "b.py#f",
          "match_method": SAME_CONTENT, "confidence": MEDIUM, "move_ambiguous": True},
         "only added or removed files"),
        ({"change": ADDED, "kind": NodeKind.FUNCTION, "to_key": "a.py#f",
          "move_ambiguous": True}, "only added or removed files"),
    ],
)  # fmt: skip
def test_invalid_node_changes(fields: dict[str, Any], message: str) -> None:
    data = {"kind": NodeKind.FILE, "confidence": HIGH, **fields}
    with pytest.raises(ValidationError, match=message):
        NodeChange.model_validate(data)


def test_node_change_key_is_the_to_key_else_the_from_key() -> None:
    moved = node(MOVED, from_key="a.py", to_key="b.py", match_method=SAME_CONTENT,
                 confidence=MEDIUM)  # fmt: skip
    assert moved.key == "b.py"
    assert node(REMOVED, from_key="a.py", confidence=HIGH).key == "a.py"


# ---------------------------------------------------------------------------
# Edge changes


def test_valid_edge_changes() -> None:
    for change in (
        edge(EdgeChangeKind.ADDED, to_ref=imports("a.py", "b.py")),
        edge(EdgeChangeKind.REMOVED, from_ref=imports("a.py", "b.py"), confidence=MEDIUM),
        edge(EdgeChangeKind.MODIFIED, imports("a.py", "b.py"), imports("a.py", "c.py")),
        edge(EdgeChangeKind.UNDETERMINED, from_ref=imports("a.py", "b.py"), confidence=LOW),
        edge(EdgeChangeKind.UNDETERMINED, to_ref=imports("a.py", "b.py"), confidence=LOW),
    ):
        assert EdgeChange.model_validate(change.model_dump()) == change


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"from_ref": imports("a.py", "b.py"), "to_ref": imports("a.py", "b.py"),
          "confidence": LOW}, "exactly one ref"),
        ({"confidence": LOW}, "exactly one ref"),
        ({"from_ref": imports("a.py", "b.py"), "confidence": MEDIUM}, "low confidence"),
    ],
)  # fmt: skip
def test_invalid_undetermined_edges(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        EdgeChange.model_validate(
            {"change": EdgeChangeKind.UNDETERMINED, "kind": EdgeKind.IMPORTS, **fields}
        )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"change": EdgeChangeKind.UNCHANGED, "from_ref": imports("a.py", "b.py"),
          "to_ref": imports("a.py", "b.py")}, "counted, not recorded"),
        ({"change": EdgeChangeKind.ADDED, "from_ref": imports("a.py", "b.py"),
          "to_ref": imports("a.py", "b.py")}, "only a to_ref"),
        ({"change": EdgeChangeKind.REMOVED, "to_ref": imports("a.py", "b.py")},
         "only a from_ref"),
        ({"change": EdgeChangeKind.MODIFIED, "to_ref": imports("a.py", "b.py")}, "both refs"),
        ({"change": EdgeChangeKind.ADDED,
          "to_ref": EdgeRef.of(EdgeKind.INHERITS, "a.py#A", "b.py#B")}, "have its kind"),
        ({"change": EdgeChangeKind.ADDED, "to_ref": imports("a.py", "b.py"), "confidence": LOW},
         "high or medium"),
    ],
)  # fmt: skip
def test_invalid_edge_changes(fields: dict[str, Any], message: str) -> None:
    data = {"kind": EdgeKind.IMPORTS, "confidence": HIGH, **fields}
    with pytest.raises(ValidationError, match=message):
        EdgeChange.model_validate(data)


# ---------------------------------------------------------------------------
# The comparison as a whole


def test_a_full_comparison_is_valid() -> None:
    c = full()
    assert SnapshotComparison.model_validate_json(c.model_dump_json()) == c


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ((same(UNCHANGED, "a.py"), node(REMOVED, from_key="a.py", confidence=HIGH)),
         "from keys appear in more than one"),
        ((node(ADDED, to_key="a.py", confidence=HIGH), node(ADDED, to_key="a.py", confidence=HIGH)),
         "to keys appear in more than one"),
        # A key present on both sides is matched by key, never removed + added ...
        ((node(REMOVED, from_key="a.py", confidence=HIGH),
          node(ADDED, to_key="a.py", confidence=HIGH)), "not matched by key"),
        # ... and never moved away while it still exists.
        ((node(MOVED, from_key="a.py", to_key="b.py", match_method=SAME_CONTENT,
               confidence=MEDIUM), node(ADDED, to_key="a.py", confidence=HIGH)),
         "not matched by key"),
        ((node(UNDETERMINED, NodeKind.FUNCTION, from_key="a.py#f", confidence=LOW),
          node(ADDED, NodeKind.FUNCTION, to_key="a.py#f", confidence=HIGH)), "not matched by key"),
    ],
)  # fmt: skip
def test_node_keys_are_unique_and_consistent_per_side(
    records: tuple[NodeChange, ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        comparison(*records)


def test_edges_are_unique_per_side() -> None:
    nodes = (same(UNCHANGED, "a.py"), same(UNCHANGED, "b.py"))
    ref = imports("a.py", "b.py")
    with pytest.raises(ValidationError, match="from edge appears in more than one"):
        comparison(*nodes, edge(EdgeChangeKind.REMOVED, from_ref=ref),
                   edge(EdgeChangeKind.MODIFIED, ref, ref))  # fmt: skip


def test_edge_ends_are_nodes_of_their_own_snapshot() -> None:
    with pytest.raises(ValidationError, match="not a node of to"):
        comparison(
            same(UNCHANGED, "a.py"), edge(EdgeChangeKind.ADDED, to_ref=imports("a.py", "x.py"))
        )
    removed_end = node(REMOVED, from_key="b.py", confidence=HIGH)
    with pytest.raises(ValidationError, match="not a node of to"):  # b.py is gone in to
        comparison(same(UNCHANGED, "a.py"), removed_end,
                   edge(EdgeChangeKind.ADDED, to_ref=imports("a.py", "b.py")))  # fmt: skip
    assert comparison(same(UNCHANGED, "a.py"), removed_end,
                      edge(EdgeChangeKind.REMOVED, from_ref=imports("a.py", "b.py")))  # fmt: skip


def test_a_modified_edge_joins_matched_ends() -> None:
    nodes = (same(UNCHANGED, "a.py"), same(UNCHANGED, "b.py"), same(UNCHANGED, "c.py"))
    with pytest.raises(ValidationError, match="not matched to each other"):
        comparison(*nodes, edge(EdgeChangeKind.MODIFIED, imports("a.py", "b.py"),
                                imports("a.py", "c.py")))  # fmt: skip


# ---------------------------------------------------------------------------
# Not fully comparable: no high confidence anywhere


INCOMPARABLE = run(FROM, identity(commit=OTHER_SHA, version="2.0.0"))


def test_not_comparable_caps_node_confidence() -> None:
    with pytest.raises(
        ValidationError, match=r"not fully comparable \(extractor_version_differs\)"
    ):
        comparison(same(UNCHANGED, "a.py"), comparison_run=INCOMPARABLE)
    c = comparison(same(UNCHANGED, "a.py", confidence=MEDIUM), comparison_run=INCOMPARABLE)
    # The lineage of a key match stays high: key equality is certain.
    assert [lin.confidence for lin in c.lineage()] == [HIGH]


def test_not_comparable_caps_edge_confidence() -> None:
    nodes = (same(UNCHANGED, "a.py", confidence=MEDIUM), same(UNCHANGED, "b.py", confidence=MEDIUM))
    added = edge(EdgeChangeKind.ADDED, to_ref=imports("a.py", "b.py"))
    with pytest.raises(ValidationError, match="not fully comparable"):
        comparison(*nodes, added, comparison_run=INCOMPARABLE)
    capped = added.model_copy(update={"confidence": MEDIUM})
    assert comparison(*nodes, capped, comparison_run=INCOMPARABLE).edges == (capped,)


# ---------------------------------------------------------------------------
# Lineage, summary, canonical form


def test_lineage_is_built_from_every_matched_node() -> None:
    lineage = full().lineage()
    assert [(lin.from_key, lin.to_key, lin.match_method, lin.confidence) for lin in lineage] == [
        ("/", "/", SAME_KEY, HIGH),
        ("app.py", "app.py", SAME_KEY, HIGH),
        ("app.py#f", "app.py#f", SAME_KEY, HIGH),  # undetermined content, certain identity
        ("old/m.py", "new/m.py", SAME_CONTENT, MEDIUM),
        ("old/m.py#A", "new/m.py#A", SAME_CONTENT, MEDIUM),
    ]
    assert all(lin.from_snapshot == FROM and lin.to_snapshot == TO for lin in lineage)
    assert full().matched_keys()["old/m.py"] == "new/m.py"


def test_summary_counts_every_kind() -> None:
    summary = full().summary()
    assert summary.nodes == {ADDED: 1, REMOVED: 1, MODIFIED: 1, MOVED: 2, UNCHANGED: 1,
                             UNDETERMINED: 1}  # fmt: skip
    assert summary.edges == {EdgeChangeKind.ADDED: 1, EdgeChangeKind.REMOVED: 1,
                             EdgeChangeKind.MODIFIED: 1, EdgeChangeKind.UNCHANGED: 3,
                             EdgeChangeKind.UNDETERMINED: 0}  # fmt: skip
    assert comparison().summary().nodes == dict.fromkeys(NodeChangeKind, 0)


def test_canonical_form_is_deterministic() -> None:
    c = full()
    shuffled = []
    for seed in range(5):
        rng = random.Random(seed)
        nodes, edges = list(c.nodes), list(c.edges)
        rng.shuffle(nodes)
        rng.shuffle(edges)
        shuffled.append(c.model_copy(update={"nodes": tuple(nodes), "edges": tuple(edges)}))
    canonical = c.canonical()
    assert all(s.canonical() == canonical for s in shuffled)
    assert canonical.canonical() == canonical
    assert [n.key for n in canonical.nodes] == [
        "/", "app.py", "app.py#f", "gone.py", "new.py", "new/m.py", "new/m.py#A",
    ]  # fmt: skip
    assert [(e.change, e.ref.target_key) for e in canonical.edges] == [
        (EdgeChangeKind.REMOVED, "gone.py"),
        (EdgeChangeKind.ADDED, "new.py"),
        (EdgeChangeKind.MODIFIED, "new/m.py"),
    ]
