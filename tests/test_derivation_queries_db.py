"""Derivation read queries and the load -> derive -> persist orchestration, against
PostgreSQL. Skipped unless ``XRAY_TEST_DATABASE_URL`` is set.

Runs are created through ``orchestration.derive_architecture`` once per module; every
query result is compared with the same question answered on the in-memory
``Derivation`` that ``derive`` returns for the same snapshot.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.architecture import ArchitectureParams, derive
from xraymyrepo.cim import Derivation, EdgeKind, Group, GroupEdge, SnapshotDocument, SnapshotStatus

from .conftest import GOLDEN_REPO, PRODUCER_HASH, SHA, reset_database

DATABASE_URL = os.environ.get("XRAY_TEST_DATABASE_URL")
psycopg = pytest.importorskip("psycopg") if DATABASE_URL else None

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="XRAY_TEST_DATABASE_URL not set")

if DATABASE_URL:
    from xraymyrepo import orchestration
    from xraymyrepo.persistence import (
        DerivationRunExistsError,
        SnapshotNotCompleteError,
        SnapshotNotFoundError,
        load_derivation,
        persist_snapshot,
    )
    from xraymyrepo.persistence import derivation_queries as dq

USERS = "group:components:pkg:pypi:acme-backend#app.users"


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    assert psycopg is not None and DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        reset_database(connection)
        yield connection


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                   commit_sha=golden.snapshot.commit_sha)  # fmt: skip


@pytest.fixture(scope="module")
def snapshots(conn: Any, golden: SnapshotDocument, analyzed: SnapshotDocument) -> dict[str, int]:
    return {"golden": persist_snapshot(conn, golden), "analyzed": persist_snapshot(conn, analyzed)}


@pytest.fixture(scope="module")
def runs(conn: Any, snapshots: dict[str, int]) -> dict[str, int]:
    return {name: orchestration.derive_architecture(conn, sid) for name, sid in snapshots.items()}


@pytest.fixture(scope="module")
def run_id(runs: dict[str, int]) -> int:
    return runs["golden"]


@pytest.fixture(scope="module")
def expected(golden: SnapshotDocument) -> Derivation:
    return derive(golden)


def paged[T, K](fetch: Callable[[K | None, int], Sequence[T]], sort_key: Callable[[T], K],
                limit: int = 2) -> list[T]:  # fmt: skip
    items: list[T] = []
    after: K | None = None
    while True:
        page = fetch(after, limit)
        items += page
        if len(page) < limit:
            return items
        after = sort_key(page[-1])


def descendants(d: Derivation, key: str) -> set[str]:
    children: dict[str | None, list[str]] = {}
    for g in d.groups:
        children.setdefault(g.parent_key, []).append(g.key)
    found, stack = {key}, [key]
    while stack:
        for child in children.get(stack.pop(), []):
            found.add(child)
            stack.append(child)
    return found


# ---------------------------------------------------------------------------
# Orchestration


def test_orchestration_stores_exactly_the_derivation(
    conn: Any, runs: dict[str, int], golden: SnapshotDocument, analyzed: SnapshotDocument
) -> None:
    assert load_derivation(conn, runs["golden"]) == derive(golden)
    assert load_derivation(conn, runs["analyzed"]) == derive(analyzed)


def test_orchestration_never_recomputes_a_stored_run(
    conn: Any, snapshots: dict[str, int], runs: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*args: Any) -> None:
        raise AssertionError("derive() called for an existing run")

    monkeypatch.setattr(orchestration, "derive", must_not_run)
    with pytest.raises(DerivationRunExistsError) as raised:
        orchestration.derive_architecture(conn, snapshots["golden"])
    assert raised.value.run_id == runs["golden"]


def test_orchestration_other_params_and_bad_snapshots(conn: Any, snapshots: dict[str, int],
                                                      runs: dict[str, int]) -> None:  # fmt: skip
    params = ArchitectureParams(name_signals={"service": "domain"})
    other = orchestration.derive_architecture(conn, snapshots["golden"], params)
    assert other not in runs.values()
    assert dq.run_params(conn, other) == params.as_json()
    with pytest.raises(SnapshotNotFoundError):
        orchestration.derive_architecture(conn, 10**9)
    repository = conn.execute("SELECT id FROM repository").fetchone()[0]
    pending = conn.execute(
        "INSERT INTO snapshot (repository_id, commit_sha, extractor_version, config_hash, "
        "cim_schema_version, config) VALUES (%s, %s, '9.0.0', %s, '1.0', '{}') RETURNING id",
        (repository, SHA, PRODUCER_HASH),
    ).fetchone()[0]
    with pytest.raises(SnapshotNotCompleteError):
        orchestration.derive_architecture(conn, pending)


# ---------------------------------------------------------------------------
# Runs


def test_runs(conn: Any, snapshots: dict[str, int], runs: dict[str, int],
              expected: Derivation) -> None:  # fmt: skip
    record = dq.get_run(conn, runs["golden"])
    assert record is not None
    assert (record.snapshot_id, record.algorithm, record.algorithm_version,
            record.params_hash, record.status) == (
        snapshots["golden"], expected.run.algorithm, expected.run.algorithm_version,
        expected.run.params_hash, SnapshotStatus.COMPLETE)  # fmt: skip
    assert dq.get_run(conn, 10**9) is None
    assert dq.run_params(conn, runs["golden"]) == ArchitectureParams().as_json()
    listed = paged(lambda a, n: dq.list_runs(conn, snapshots["golden"], after=a, limit=n),
                   lambda r: r.id, limit=1)  # fmt: skip
    assert listed[-1].id == runs["golden"]  # newest first; the oldest is the default run
    assert [r.id for r in listed] == sorted((r.id for r in listed), reverse=True)


def test_lenses_coverage_and_finding_counts(conn: Any, run_id: int, expected: Derivation) -> None:
    lenses = {lens.name: lens for lens in dq.run_lenses(conn, run_id)}
    assert set(lenses) == {"components", "directory"}
    for name, lens in lenses.items():
        assert lens.groups == sum(g.lens == name for g in expected.groups)
        assert lens.members == sum(m.group_key.startswith(f"group:{name}:")
                                   for m in expected.members)  # fmt: skip
    coverage = dq.layer_coverage(conn, run_id)
    considered = [m for m in expected.metrics if m.metric == "layer_considered"]
    assert (coverage.considered, coverage.classified, coverage.unclassified) == (
        len(considered),
        len(expected.layers),
        len(considered) - len(expected.layers),
    )
    assert coverage.by_layer == dict(sorted(Counter(x.layer for x in expected.layers).items()))
    assert coverage.by_confidence == Counter(x.confidence for x in expected.layers)
    assert dq.finding_counts(conn, run_id) == Counter(f.rule for f in expected.findings)


# ---------------------------------------------------------------------------
# Groups


def test_group_tree_traversal(conn: Any, run_id: int, expected: Derivation) -> None:
    """Expanding every group, two per page, from each lens's top level reproduces the
    group trees exactly, in key order."""
    for lens in ("components", "directory"):
        seen: list[Group] = []
        frontier: list[str | None] = [None]
        while frontier:
            parent = frontier.pop()

            def fetch(
                after: str | None, limit: int, parent: str | None = parent, lens: str = lens
            ) -> list[Group]:
                return dq.group_children(conn, run_id, lens, parent, after=after, limit=limit)

            children = paged(fetch, lambda g: g.key)
            assert children == sorted(
                (g for g in expected.groups if g.lens == lens and g.parent_key == parent),
                key=lambda g: g.key)  # fmt: skip
            seen += children
            frontier += [g.key for g in children]
        assert sorted(g.key for g in seen) == sorted(g.key for g in expected.groups
                                                     if g.lens == lens)  # fmt: skip


def test_groups_by_key(conn: Any, run_id: int, expected: Derivation) -> None:
    keys = [g.key for g in expected.groups]
    counts = dq.group_counts(conn, run_id, keys)
    metrics = dq.group_metrics(conn, run_id, keys)
    mixes = dq.layer_mix(conn, run_id, keys)
    considered = {m.node_key for m in expected.metrics if m.metric == "layer_considered"}
    layers = {x.node_key: x.layer for x in expected.layers}
    for group in expected.groups:
        assert dq.get_group(conn, run_id, group.key) == group
        children = sum(g.parent_key == group.key for g in expected.groups)
        direct = sum(m.group_key == group.key for m in expected.members)
        assert counts[group.key] == (children, direct), group.key
        assert metrics[group.key] == {m.metric: m.value for m in expected.metrics
                                      if m.node_key == group.key}  # fmt: skip
        subtree = descendants(expected, group.key)
        files = {m.node_key for m in expected.members if m.group_key in subtree} & considered
        mix = mixes[group.key]
        assert mix.by_layer == dict(sorted(Counter(layers[f] for f in files if f in layers)
                                           .items()))  # fmt: skip
        assert mix.unclassified == sum(f not in layers for f in files), group.key
    assert dq.get_group(conn, run_id, "group:components:nope") is None
    assert [a.key for a in dq.group_ancestors(conn, run_id, USERS)] == [
        "group:components:pkg:pypi:acme-backend"]  # fmt: skip
    path = dq.group_ancestors(conn, run_id, "group:directory:backend/app/users")
    assert [a.label for a in path] == ["golden-fixture", "backend", "app"]


def test_group_members(conn: Any, run_id: int, expected: Derivation) -> None:
    for group in expected.groups:

        def fetch(after: str | None, limit: int, key: str = group.key) -> list[Any]:
            return dq.group_members(conn, run_id, key, after=after, limit=limit)

        members = paged(fetch, lambda n: n.key)
        assigned = sorted(m.node_key for m in expected.members if m.group_key == group.key)
        assert [n.key for n in members] == assigned, group.key


# ---------------------------------------------------------------------------
# Group edges


def test_zoom_levels_partition_the_group_edges(conn: Any, run_id: int,
                                               expected: Derivation) -> None:  # fmt: skip
    """Every group edge connects siblings, so the top levels plus "within" every group
    return each group edge exactly once."""
    sort_key = dq.group_edge_sort_key

    def level(**where: Any) -> list[GroupEdge]:
        def fetch(after: dq.GroupEdgeSortKey | None, limit: int) -> list[GroupEdge]:
            return dq.group_edges(conn, run_id, after=after, limit=limit, **where)

        return paged(fetch, sort_key)

    found: list[GroupEdge] = []
    for lens in ("components", "directory"):
        found += level(lens=lens)
    for group in expected.groups:
        page = level(within=group.key)
        parent = {e.source_group_key for e in page} | {e.target_group_key for e in page}
        assert all(next(g for g in expected.groups if g.key == k).parent_key == group.key
                   for k in parent)  # fmt: skip
        found += page
    assert sorted(found, key=sort_key) == sorted(expected.group_edges, key=sort_key)
    assert len(found) == len(expected.group_edges)


def test_edges_of_one_group(conn: Any, run_id: int, expected: Derivation) -> None:
    out = dq.group_edges(conn, run_id, group=USERS, direction="out", limit=100)
    into = dq.group_edges(conn, run_id, group=USERS, direction="in", limit=100)
    both = dq.group_edges(conn, run_id, group=USERS, limit=100)
    assert out == [e for e in expected.group_edges if e.source_group_key == USERS]
    assert into == [e for e in expected.group_edges if e.target_group_key == USERS]
    key = dq.group_edge_sort_key
    assert sorted(both, key=key) == sorted(out + into, key=key)
    tests = dq.group_edges(conn, run_id, group=USERS, kinds=[EdgeKind.TESTS], limit=100)
    assert {e.edge_kind for e in tests} == {EdgeKind.TESTS}


# ---------------------------------------------------------------------------
# Layers and findings


def test_layers(conn: Any, run_id: int, expected: Derivation, golden: SnapshotDocument) -> None:
    considered = {m.node_key for m in expected.metrics if m.metric == "layer_considered"}
    layers = {x.node_key: x for x in expected.layers}
    for node in golden.nodes:
        assert dq.get_layer(conn, run_id, node.key) == layers.get(node.key)
        assert dq.is_considered(conn, run_id, node.key) == (node.key in considered)
    listed = paged(lambda a, n: dq.list_layers(conn, run_id, after=a, limit=n),
                   lambda x: x.node_key)  # fmt: skip
    assert listed == sorted(expected.layers, key=lambda x: x.node_key)
    interface = dq.list_layers(conn, run_id, layer="interface", limit=100)
    assert {x.node_key for x in interface} == {k for k, x in layers.items()
                                               if x.layer == "interface"}  # fmt: skip
    unclassified = paged(lambda a, n: dq.list_unclassified(conn, run_id, after=a, limit=n),
                         lambda b: b.key)  # fmt: skip
    assert [b.key for b in unclassified] == sorted(considered - set(layers))


def test_findings(conn: Any, run_id: int, expected: Derivation) -> None:
    listed = paged(lambda a, n: dq.list_findings(conn, run_id, after=a, limit=n),
                   lambda item: item[0], limit=1)  # fmt: skip
    assert [f for _, f in listed] == list(expected.findings)
    cycles = dq.list_findings(conn, run_id, rule="arch.group.cycle", limit=10)
    assert [f.rule for _, f in cycles] == ["arch.group.cycle"]
    assert dq.list_findings(conn, run_id, rule="arch.layer.violation", limit=10) == []
