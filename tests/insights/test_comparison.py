"""Snapshot comparison (no database): the reviewed fixture, and synthetic snapshot pairs
for every matching and classification rule."""

from __future__ import annotations

import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.analyzer import DEFAULT_IGNORE_GLOBS, AnalysisOptions, analyze
from xraymyrepo.cim import (
    Confidence,
    EdgeChangeKind,
    Incomparability,
    MatchMethod,
    NodeChange,
    NodeChangeKind,
    NodeKind,
    RepositoryRef,
    SnapshotComparison,
    SnapshotDocument,
)
from xraymyrepo.insights.comparison import (
    ALGORITHM,
    ALGORITHM_VERSION,
    ComparisonContractError,
    ComparisonParams,
    compare,
    validate_against,
)

from ..analyzer.helpers import write_repo
from ..conftest import ROOT

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
FIXTURE_REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
REPO = RepositoryRef(host="local", owner="tests", name="sample")
BEFORE_SHA, AFTER_SHA = "1" * 40, "2" * 40

ADDED, REMOVED, MODIFIED = NodeChangeKind.ADDED, NodeChangeKind.REMOVED, NodeChangeKind.MODIFIED
MOVED, UNCHANGED = NodeChangeKind.MOVED, NodeChangeKind.UNCHANGED
UNDETERMINED = NodeChangeKind.UNDETERMINED
HIGH, MEDIUM, LOW = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW


def snapshot(
    root: Path, files: Mapping[str, str | bytes], sha: str, options: AnalysisOptions | None = None
) -> SnapshotDocument:
    return analyze(write_repo(root, dict(files)), repository=REPO, commit_sha=sha, options=options)


def pair(
    tmp_path: Path,
    before: Mapping[str, str | bytes],
    after: Mapping[str, str | bytes],
    after_options: AnalysisOptions | None = None,
) -> tuple[SnapshotDocument, SnapshotDocument]:
    return (
        snapshot(tmp_path / "before", before, BEFORE_SHA),
        snapshot(tmp_path / "after", after, AFTER_SHA, after_options),
    )


def changes(c: SnapshotComparison) -> dict[str, tuple[NodeChangeKind, Confidence]]:
    """key ('from -> to' for moves) -> (change, confidence), unchanged rows left out."""
    found = {}
    for n in c.nodes:
        if n.change is UNCHANGED:
            continue
        key = f"{n.from_key} -> {n.to_key}" if n.change is MOVED else n.key
        found[key] = (n.change, n.confidence)
    return found


def edge_changes(c: SnapshotComparison) -> list[tuple[str, str, str, str]]:
    return [
        (e.change.value, e.ref.source_key, e.ref.target_key, e.confidence.value) for e in c.edges
    ]


def row(c: SnapshotComparison, key: str) -> NodeChange:
    return next(n for n in c.nodes if key in (n.from_key, n.to_key))


def compare_files(
    tmp_path: Path, before: Mapping[str, str | bytes], after: Mapping[str, str | bytes]
) -> SnapshotComparison:
    return compare(*pair(tmp_path, before, after))


# ---------------------------------------------------------------------------
# The reviewed fixture


@pytest.fixture(scope="module")
def fixture_docs() -> tuple[SnapshotDocument, SnapshotDocument]:
    return (
        analyze(FIXTURE / "before", repository=FIXTURE_REPO, commit_sha=BEFORE_SHA),
        analyze(FIXTURE / "after", repository=FIXTURE_REPO, commit_sha=AFTER_SHA),
    )


def test_fixture_matches_the_reviewed_expectation(
    fixture_docs: tuple[SnapshotDocument, SnapshotDocument],
) -> None:
    expected = SnapshotComparison.model_validate_json(
        (FIXTURE / "expected_comparison.json").read_text(encoding="utf-8")
    )
    assert compare(*fixture_docs) == expected


def test_fixture_cases(fixture_docs: tuple[SnapshotDocument, SnapshotDocument]) -> None:
    """The cases of fixtures/comparison/v1/FIXTURE.md, spelled out independently."""
    c = compare(*fixture_docs)
    assert c.run.comparable
    assert (c.run.algorithm, c.run.algorithm_version) == (ALGORITHM, ALGORITHM_VERSION)
    found = changes(c)
    # A real code change versus a comment/whitespace-only change.
    assert found["shop/pricing.py#price_with_tax"] == (MODIFIED, HIGH)
    assert row(c, "shop/pricing.py#Cart").change is UNCHANGED
    assert found["shop/util.py"] == (MODIFIED, HIGH)  # the bytes changed ...
    assert row(c, "shop/util.py#slugify").change is UNCHANGED  # ... the code did not
    # Added and deleted files, with their declarations.
    assert found["shop/catalog.py"] == found["shop/catalog.py#find"] == (ADDED, HIGH)
    assert found["shop/legacy.py"] == found["shop/legacy.py#old_discount"] == (REMOVED, HIGH)
    # An identical-content move, its function moving with it.
    assert found["shop/old/shipping.py -> shop/new/shipping.py"] == (MOVED, MEDIUM)
    assert found["shop/old/shipping.py#shipping_cost -> shop/new/shipping.py#shipping_cost"] == (
        MOVED,
        MEDIUM,
    )
    # Ambiguous: one removed, two identical added candidates. Never guessed.
    for key in ("shop/a/template.py", "shop/b/template.py", "shop/c/template.py"):
        assert row(c, key).change in (ADDED, REMOVED) and row(c, key).move_ambiguous
    # Trivial: no declarations, so not move-matched and not flagged.
    assert found["shop/consts.py"] == (REMOVED, HIGH)
    assert found["shop/settings/consts.py"] == (ADDED, HIGH)
    assert not row(c, "shop/consts.py").move_ambiguous
    # A file that stopped parsing: its function is undetermined, not removed.
    assert found["shop/report.py#summarize"] == (UNDETERMINED, LOW)
    assert edge_changes(c) == [
        # the import was rewritten to the new path (its specifier changed)
        ("modified", "shop/checkout.py", "shop/new/shipping.py#shipping_cost", "medium"),
        ("added", "shop/pricing.py", "shop/catalog.py#find", "high"),
        ("removed", "shop/pricing.py", "shop/legacy.py#old_discount", "high"),
        ("undetermined", "shop/report.py", "shop/pricing.py#Cart", "low"),
    ]
    assert c.unchanged_edges == 1  # pricing.py -> util.py#slugify
    lineage = {(lin.from_key, lin.to_key): lin for lin in c.lineage()}
    moved = lineage[("shop/old/shipping.py", "shop/new/shipping.py")]
    assert (moved.match_method, moved.confidence) == (MatchMethod.SAME_CONTENT_HASH, MEDIUM)


# ---------------------------------------------------------------------------
# Synthetic pairs

SERVICE = "def total(items):\n    return sum(items)\n\n\ndef count(items):\n    return len(items)\n"


def test_identical_snapshots_change_nothing(tmp_path: Path) -> None:
    files = {"a.py": "from .b import f\n", "b.py": "def f():\n    pass\n"}
    before, after = pair(tmp_path, files, files)
    c = compare(before, after)
    assert {n.change for n in c.nodes} == {UNCHANGED}
    assert c.edges == () and c.unchanged_edges == len(before.edges) == 1


def test_a_modified_function(tmp_path: Path) -> None:
    c = compare_files(
        tmp_path, {"s.py": SERVICE}, {"s.py": SERVICE.replace("sum(items)", "sum(items, 0)")}
    )
    assert changes(c) == {"s.py": (MODIFIED, HIGH), "s.py#total": (MODIFIED, HIGH)}
    assert row(c, "s.py#count").change is UNCHANGED


def test_comments_and_whitespace_change_the_file_only(tmp_path: Path) -> None:
    reformatted = "# Totals.\ndef total( items ):  # sum\n\n    return sum(items)\n\n\n" + (
        "def count(items):\n    return len(items)\n"
    )
    c = compare_files(tmp_path, {"s.py": SERVICE}, {"s.py": reformatted})
    assert changes(c) == {"s.py": (MODIFIED, HIGH)}


def test_a_moved_declaration_inside_its_file_is_unchanged(tmp_path: Path) -> None:
    swapped = (
        "def count(items):\n    return len(items)\n\n\ndef total(items):\n    return sum(items)\n"
    )
    c = compare_files(tmp_path, {"s.py": SERVICE}, {"s.py": swapped})
    assert changes(c) == {"s.py": (MODIFIED, HIGH)}  # spans are not content


def test_added_and_deleted_files_and_their_edges(tmp_path: Path) -> None:
    lib = "def helper():\n    pass\n"
    c = compare_files(
        tmp_path,
        {"app.py": "from .old import helper\n", "old.py": lib},
        {"app.py": "from .new import helper\n", "new.py": lib + "\n\ndef extra():\n    pass\n"},
    )
    found = changes(c)
    assert found["old.py"] == found["old.py#helper"] == (REMOVED, HIGH)
    assert found["new.py"] == found["new.py#helper"] == found["new.py#extra"] == (ADDED, HIGH)
    assert edge_changes(c) == [
        ("added", "app.py", "new.py#helper", "high"),
        ("removed", "app.py", "old.py#helper", "high"),
    ]


def test_an_identical_move_keeps_its_edges(tmp_path: Path) -> None:
    mover = "from ..util import tool\n\n\ndef run():\n    return tool()\n"
    util = "def tool():\n    pass\n"
    c = compare_files(
        tmp_path,
        {"pkg/a/m.py": mover, "pkg/util.py": util},
        {"pkg/b/m.py": mover, "pkg/util.py": util},
    )
    assert changes(c) == {
        "pkg/a": (REMOVED, HIGH),
        "pkg/b": (ADDED, HIGH),
        "pkg/a/m.py -> pkg/b/m.py": (MOVED, MEDIUM),
        "pkg/a/m.py#run -> pkg/b/m.py#run": (MOVED, MEDIUM),
    }
    # The import out of the moved file is the same edge under the matched keys.
    assert c.edges == () and c.unchanged_edges == 1
    assert c.matched_keys()["pkg/a/m.py#run"] == "pkg/b/m.py#run"


def test_moved_declarations_pair_only_on_the_same_key_and_kind(tmp_path: Path) -> None:
    content = "class A:\n    def f(self):\n        pass\n"
    c = compare_files(tmp_path, {"old.py": content}, {"new.py": content})
    assert {(n.from_key, n.to_key) for n in c.nodes if n.change is MOVED} == {
        ("old.py", "new.py"),
        ("old.py#A", "new.py#A"),
        ("old.py#A.f", "new.py#A.f"),
    }


def test_a_move_with_an_edit_is_removed_and_added(tmp_path: Path) -> None:
    c = compare_files(tmp_path, {"old.py": SERVICE}, {"new.py": SERVICE + "\nX = 1\n"})
    assert changes(c)["old.py"] == (REMOVED, HIGH) and changes(c)["new.py"] == (ADDED, HIGH)
    assert not row(c, "old.py").move_ambiguous  # different content: not a candidate


@pytest.mark.parametrize(
    ("before", "after", "ambiguous"),
    [
        # two candidates on the to side
        ({"old.py": SERVICE}, {"a.py": SERVICE, "b.py": SERVICE}, {"old.py", "a.py", "b.py"}),
        # the content also stays where it was (a copy, then a delete)
        ({"keep.py": SERVICE, "old.py": SERVICE}, {"keep.py": SERVICE, "new.py": SERVICE},
         {"old.py", "new.py"}),
    ],
)  # fmt: skip
def test_ambiguous_moves_are_never_guessed(
    tmp_path: Path, before: dict[str, str], after: dict[str, str], ambiguous: set[str]
) -> None:
    c = compare_files(tmp_path, before, after)
    assert not any(n.change is MOVED for n in c.nodes)
    assert {n.key for n in c.nodes if n.move_ambiguous} == ambiguous


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("empty.py", ""),  # the empty blob
        ("consts.py", "RATE = 0.2\nNAMES = ['a', 'b']\n"),  # no declarations
        ("notes.md", "# Notes\n\nSome text.\n"),  # not Python
        ("pyproject.toml", '[project]\nname = "x"\n'),  # not Python
    ],
)
def test_trivial_and_non_python_files_are_not_move_matched(
    tmp_path: Path, name: str, content: str
) -> None:
    c = compare_files(tmp_path, {f"a/{name}": content}, {f"b/{name}": content})
    assert not any(n.change is MOVED for n in c.nodes)
    assert not any(n.move_ambiguous for n in c.nodes)
    assert changes(c)[f"a/{name}"][0] is REMOVED and changes(c)[f"b/{name}"][0] is ADDED


def test_the_move_threshold_is_a_parameter(tmp_path: Path) -> None:
    before, after = pair(tmp_path, {"a/m.py": SERVICE}, {"b/m.py": SERVICE})
    assert any(n.change is MOVED for n in compare(before, after).nodes)
    strict = ComparisonParams(min_move_symbols=3)  # SERVICE declares two functions
    c = compare(before, after, strict)
    assert not any(n.change is MOVED for n in c.nodes)
    assert c.run.params_hash == strict.params_hash() != ComparisonParams().params_hash()


def test_a_kind_change_under_one_key_is_modified(tmp_path: Path) -> None:
    c = compare_files(tmp_path, {"x/a.py": "A = 1\n"}, {"x": "plain file\n"})
    assert changes(c)["x"] == (MODIFIED, HIGH)
    assert row(c, "x").kind is NodeKind.FILE


# ---------------------------------------------------------------------------
# Extraction failures: undetermined, never guessed


def test_a_file_that_stops_parsing(tmp_path: Path) -> None:
    good = "from .lib import f\n\n\ndef g():\n    return f()\n"
    c = compare_files(
        tmp_path,
        {"m.py": good, "lib.py": "def f(): pass\n"},
        {"m.py": good.replace("def g()", "def g("), "lib.py": "def f(): pass\n"},
    )
    assert changes(c) == {"m.py": (MODIFIED, HIGH), "m.py#g": (UNDETERMINED, LOW)}
    assert edge_changes(c) == [("undetermined", "m.py", "lib.py#f", "low")]


def test_a_file_that_starts_parsing(tmp_path: Path) -> None:
    good = "from .lib import f\n\n\ndef g():\n    return f()\n"
    c = compare_files(
        tmp_path,
        {"m.py": good.replace("def g()", "def g("), "lib.py": "def f(): pass\n"},
        {"m.py": good, "lib.py": "def f(): pass\n"},
    )
    assert changes(c) == {"m.py": (MODIFIED, HIGH), "m.py#g": (UNDETERMINED, LOW)}
    assert row(c, "m.py#g").from_key is None
    assert edge_changes(c) == [("undetermined", "m.py", "lib.py#f", "low")]


def test_imports_of_a_vanished_declaration_are_undetermined(tmp_path: Path) -> None:
    lib = "def f():\n    pass\n"
    c = compare_files(
        tmp_path,
        {"app.py": "from .lib import f\n", "lib.py": lib},
        {"app.py": "from .lib import f\n", "lib.py": "def f(:\n"},
    )
    assert changes(c)["lib.py#f"] == (UNDETERMINED, LOW)
    # The import of f now resolves to the unparsed file itself: not a new import.
    assert edge_changes(c) == [
        ("undetermined", "app.py", "lib.py", "low"),
        ("undetermined", "app.py", "lib.py#f", "low"),
    ]


def test_a_new_import_of_a_file_unparsed_on_both_sides_is_added(tmp_path: Path) -> None:
    broken = "def f(:\n"
    c = compare_files(
        tmp_path,
        {"app.py": "X = 1\n", "lib.py": broken},
        {"app.py": "from . import lib\n", "lib.py": broken},
    )
    assert edge_changes(c) == [("added", "app.py", "lib.py", "high")]


# ---------------------------------------------------------------------------
# Not fully comparable


def with_identity(
    doc: SnapshotDocument, *, strip_fingerprints: bool = False, **identity: Any
) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    if strip_fingerprints:
        for node in data["nodes"]:
            node["content_hash"] = node["signature_hash"] = None
    return SnapshotDocument.model_validate(data)


def test_a_changed_extractor_version(tmp_path: Path) -> None:
    """An older extractor without fingerprints: identical files are unchanged, symbols
    of changed files are undetermined, nothing is high, key lineage stays high."""
    before, after = pair(
        tmp_path,
        {"same.py": SERVICE, "edit.py": SERVICE},
        {"same.py": SERVICE, "edit.py": SERVICE.replace("sum", "max")},
    )
    old = with_identity(before, strip_fingerprints=True, extractor_version="0.1.0")
    c = compare(old, after)
    assert c.run.incomparable == (Incomparability.EXTRACTOR_VERSION_DIFFERS,)
    assert {n.confidence for n in c.nodes} <= {MEDIUM, LOW}
    assert row(c, "same.py#total").change is UNCHANGED
    assert row(c, "same.py#total").confidence is MEDIUM
    assert row(c, "edit.py").change is MODIFIED
    assert changes(c)["edit.py#total"] == changes(c)["edit.py#count"] == (UNDETERMINED, LOW)
    assert {lin.confidence for lin in c.lineage()} == {HIGH}


def test_fingerprints_are_not_compared_across_extractor_versions(tmp_path: Path) -> None:
    before, after = pair(tmp_path, {"s.py": SERVICE}, {"s.py": SERVICE + "\n# end\n"})
    c = compare(with_identity(before, extractor_version="0.1.9"), after)
    # Equal fingerprints, but from another extractor: the file changed, so undetermined.
    assert changes(c)["s.py#total"] == (UNDETERMINED, LOW)


def test_missing_fingerprints_under_one_extractor_fall_back_to_the_file(tmp_path: Path) -> None:
    before, after = pair(
        tmp_path,
        {"same.py": SERVICE, "edit.py": SERVICE},
        {"same.py": SERVICE, "edit.py": SERVICE + "\n# end\n"},
    )
    c = compare(with_identity(before, strip_fingerprints=True), after)
    assert c.run.comparable
    assert row(c, "same.py#total").change is UNCHANGED
    assert row(c, "same.py#total").confidence is HIGH
    assert changes(c)["edit.py#total"] == (UNDETERMINED, LOW)


def test_a_changed_configuration(tmp_path: Path) -> None:
    files = {"s.py": SERVICE, "gen/x.py": "def x(): pass\n", "u.py": "from .gen.x import x\n"}
    edited = {**files, "s.py": SERVICE.replace("sum", "max")}
    narrower = AnalysisOptions(ignore_globs=(*DEFAULT_IGNORE_GLOBS, "gen/**"))
    before, after = pair(tmp_path, files, edited, after_options=narrower)
    c = compare(before, after)
    assert c.run.incomparable == (Incomparability.CONFIG_DIFFERS,)
    # Same extractor: fingerprints are still compared, capped at medium.
    assert changes(c)["s.py#total"] == (MODIFIED, MEDIUM)
    assert row(c, "s.py#count").change is UNCHANGED
    assert changes(c)["gen/x.py"] == (REMOVED, MEDIUM)  # ignored now, not deleted
    assert {e.confidence for e in c.edges} <= {MEDIUM, LOW}
    assert all(n.confidence is not HIGH for n in c.nodes)


def test_both_reasons_are_recorded_in_enum_order(tmp_path: Path) -> None:
    narrower = AnalysisOptions(ignore_globs=(*DEFAULT_IGNORE_GLOBS, "gen/**"))
    before, after = pair(tmp_path, {"s.py": SERVICE}, {"s.py": SERVICE}, after_options=narrower)
    c = compare(with_identity(before, extractor_version="0.1.9"), after)
    assert c.run.incomparable == (
        Incomparability.EXTRACTOR_VERSION_DIFFERS,
        Incomparability.CONFIG_DIFFERS,
    )
    assert row(c, "s.py#total").change is UNCHANGED  # same blob: unchanged, at most medium
    assert all(n.confidence is not HIGH for n in c.nodes)


def test_only_move_languages_are_move_matched(tmp_path: Path) -> None:
    before, after = pair(tmp_path, {"a/m.py": SERVICE}, {"b/m.py": SERVICE})
    c = compare(before, after, ComparisonParams(move_languages=()))
    assert not any(n.change is MOVED or n.move_ambiguous for n in c.nodes)
    assert changes(c)["a/m.py"] == (REMOVED, HIGH) and changes(c)["b/m.py"] == (ADDED, HIGH)


# ---------------------------------------------------------------------------
# Determinism and checks


def shuffled(doc: SnapshotDocument, seed: int) -> SnapshotDocument:
    rng = random.Random(seed)
    nodes, edges = list(doc.nodes), list(doc.edges)
    rng.shuffle(nodes)
    rng.shuffle(edges)
    return doc.model_copy(update={"nodes": tuple(nodes), "edges": tuple(edges)})


def test_input_order_does_not_matter(
    fixture_docs: tuple[SnapshotDocument, SnapshotDocument],
) -> None:
    expected = compare(*fixture_docs)
    before, after = fixture_docs
    for seed in range(5):
        assert compare(shuffled(before, seed), shuffled(after, seed + 100)) == expected
    assert expected.canonical() == expected


def test_validate_against_rejects_comparisons_that_do_not_fit(
    fixture_docs: tuple[SnapshotDocument, SnapshotDocument],
) -> None:
    before, after = fixture_docs
    good = compare(before, after)
    validate_against(before, after, good)

    def broken(**update: Any) -> SnapshotComparison:
        return good.model_copy(update=update)

    with pytest.raises(ComparisonContractError, match="names other snapshots"):
        validate_against(after, before, good)
    with pytest.raises(ComparisonContractError, match="do not cover the from snapshot"):
        validate_against(before, after, broken(nodes=good.nodes[1:]))
    with pytest.raises(ComparisonContractError, match="every edge of from"):
        validate_against(before, after, broken(unchanged_edges=good.unchanged_edges + 1))
    with pytest.raises(ComparisonContractError, match="not an edge of to"):
        added = next(e for e in good.edges if e.change is EdgeChangeKind.ADDED)
        assert added.to_ref is not None
        fake = added.model_copy(
            update={"to_ref": added.to_ref.model_copy(update={"target_key": "shop/util.py"})}
        )
        validate_against(before, after, broken(edges=(*good.edges[:1], fake, *good.edges[2:])))
    # A moved file must keep its content.
    other_before = with_identity(before)  # same identity, then edit one blob
    data = other_before.model_dump(mode="json")
    for node in data["nodes"]:
        if node["key"] == "shop/old/shipping.py":
            node["blob_sha"] = "0" * 40
    with pytest.raises(ComparisonContractError, match="changed content"):
        validate_against(SnapshotDocument.model_validate(data), after, good)
    # A row must name a node of its kind.
    renamed = tuple(
        n.model_copy(update={"kind": NodeKind.DIRECTORY}) if n.key == "shop/legacy.py" else n
        for n in good.nodes
    )
    with pytest.raises(ComparisonContractError, match="is a file, not a directory"):
        validate_against(before, after, broken(nodes=renamed))
