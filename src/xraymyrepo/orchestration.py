"""Load -> derive -> persist: the one place that combines the derivation algorithms with
persistence.

``architecture`` and ``insights`` are pure (CIM in, CIM out) and ``persistence``
knows nothing about how derivations, comparisons or change risk are computed; this
module wires them for callers. No CLI in this milestone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from xraymyrepo.cim import Derivation, SnapshotDocument, SnapshotStatus

from . import architecture
from .architecture import ArchitectureParams, derive
from .insights import change_risk as change_risk_algorithm
from .insights import comparison as comparison_algorithm
from .insights import health as health_algorithm
from .insights.change_risk import ChangeRiskParams, change_risk
from .insights.comparison import ComparisonParams, compare
from .insights.health import HealthParams, health
from .persistence import (
    ChangeRiskExistsError,
    ComparisonExistsError,
    ComparisonNotCompleteError,
    ComparisonNotFoundError,
    DerivationRunExistsError,
    SnapshotNotCompleteError,
    SnapshotNotFoundError,
    comparison_queries,
    find_change_risk,
    find_comparison,
    find_derivation_run,
    load_comparison,
    load_snapshot,
    persist_change_risk,
    persist_comparison,
    persist_derivation,
    queries,
)
from .persistence.snapshots import Connection


class SnapshotsNotComparableError(ValueError):
    """Two stored snapshots that cannot be compared at all: the same snapshot, or
    snapshots of different repositories. (Snapshots that differ in extractor version or
    configuration *are* compared, as not fully comparable.)"""


def _derive_and_store(
    conn: Connection,
    snapshot_id: int,
    identity: tuple[str, str, str],
    params: Mapping[str, Any],
    compute: Callable[[SnapshotDocument], Derivation],
) -> int:
    """Store ``compute(snapshot)`` unless a run with this identity exists; derivations
    are deterministic, so an existing run is never recomputed or replaced."""
    existing = find_derivation_run(conn, snapshot_id, *identity)
    if existing is not None:
        raise DerivationRunExistsError(existing.id, existing.status)
    return persist_derivation(conn, snapshot_id, compute(load_snapshot(conn, snapshot_id)), params)


def derive_architecture(
    conn: Connection, snapshot_id: int, params: ArchitectureParams | None = None
) -> int:
    """Derive the architecture of a stored, complete snapshot and store it; returns the
    run id.

    Raises ``SnapshotNotFoundError`` / ``SnapshotNotCompleteError`` for an unusable
    snapshot and ``DerivationRunExistsError`` when this (snapshot, algorithm version,
    params) run is already stored.
    """
    params = params or ArchitectureParams()
    identity = (architecture.ALGORITHM, architecture.ALGORITHM_VERSION, params.params_hash())
    return _derive_and_store(
        conn, snapshot_id, identity, params.as_json(), lambda doc: derive(doc, params)
    )


def derive_health(conn: Connection, snapshot_id: int, params: HealthParams | None = None) -> int:
    """Derive the health findings and metrics of a stored, complete snapshot and store
    them; returns the run id. Same errors as ``derive_architecture``."""
    params = params or HealthParams()
    identity = (
        health_algorithm.ALGORITHM,
        health_algorithm.ALGORITHM_VERSION,
        params.params_hash(),
    )
    return _derive_and_store(
        conn, snapshot_id, identity, params.as_json(), lambda doc: health(doc, params)
    )


def compare_snapshots(
    conn: Connection,
    from_snapshot_id: int,
    to_snapshot_id: int,
    params: ComparisonParams | None = None,
) -> int:
    """Compare two stored, complete snapshots of one repository (``from`` -> ``to``) and
    store the comparison; returns its id.

    Raises ``SnapshotNotFoundError`` / ``SnapshotNotCompleteError`` for an unusable
    snapshot, ``SnapshotsNotComparableError`` for the same snapshot twice or snapshots of
    different repositories, and ``ComparisonExistsError`` when this (from, to, algorithm
    version, params) comparison is already stored, in any status. Both checks run
    before either snapshot is loaded: comparisons are deterministic, so an existing one
    is never recomputed or replaced. Snapshots that are not fully comparable are compared
    and stored with their ``incomparable`` reasons. Storing is one transaction
    (``persist_comparison``); loading the immutable snapshots needs none.
    """
    params = params or ComparisonParams()
    snapshots = []
    for snapshot_id in (from_snapshot_id, to_snapshot_id):
        snapshot = queries.get_snapshot(conn, snapshot_id)
        if snapshot is None:
            raise SnapshotNotFoundError(f"snapshot {snapshot_id} does not exist")
        if snapshot.status is not SnapshotStatus.COMPLETE:
            raise SnapshotNotCompleteError(f"snapshot {snapshot_id} is {snapshot.status}")
        snapshots.append(snapshot.identity)
    if from_snapshot_id == to_snapshot_id:
        raise SnapshotsNotComparableError(
            f"snapshot {from_snapshot_id} cannot be compared with itself"
        )
    if snapshots[0].repository != snapshots[1].repository:
        raise SnapshotsNotComparableError(
            f"snapshots {from_snapshot_id} and {to_snapshot_id} belong to different repositories"
        )
    identity = (
        comparison_algorithm.ALGORITHM,
        comparison_algorithm.ALGORITHM_VERSION,
        params.params_hash(),
    )
    existing = find_comparison(conn, from_snapshot_id, to_snapshot_id, *identity)
    if existing is not None:
        raise ComparisonExistsError(existing.id, existing.status)
    comparison = compare(
        load_snapshot(conn, from_snapshot_id), load_snapshot(conn, to_snapshot_id), params
    )
    return persist_comparison(conn, from_snapshot_id, to_snapshot_id, comparison, params.as_json())


def assess_change_risk(
    conn: Connection, comparison_id: int, params: ChangeRiskParams | None = None
) -> int:
    """Assess the change risk of a stored, complete comparison and store it; returns the
    run id.

    Raises ``ComparisonNotFoundError`` / ``ComparisonNotCompleteError`` for an unusable
    comparison, and ``ChangeRiskExistsError`` when this (comparison, algorithm version,
    params) run is already stored, in any status. Both checks run before anything is
    loaded: assessments are deterministic, so an existing one is never recomputed or
    replaced. A comparison that is not fully comparable is assessed, with the
    ``incomparable_snapshots`` caveat. The comparison and both of its snapshots are
    loaded (removed files are assessed in ``from``, the rest in ``to``); health is
    computed inline by the algorithm. Storing is one transaction
    (``persist_change_risk``); loading the immutable records needs none.
    """
    params = params or ChangeRiskParams()
    record = comparison_queries.get_comparison(conn, comparison_id)
    if record is None:
        raise ComparisonNotFoundError(f"snapshot comparison {comparison_id} does not exist")
    if record.status is not SnapshotStatus.COMPLETE:
        raise ComparisonNotCompleteError(f"snapshot comparison {comparison_id} is {record.status}")
    identity = (
        change_risk_algorithm.ALGORITHM,
        change_risk_algorithm.ALGORITHM_VERSION,
        params.params_hash(),
    )
    existing = find_change_risk(conn, comparison_id, *identity)
    if existing is not None:
        raise ChangeRiskExistsError(existing.id, existing.status)
    assessment = change_risk(
        load_comparison(conn, comparison_id),
        load_snapshot(conn, record.from_snapshot_id),
        load_snapshot(conn, record.to_snapshot_id),
        params,
    )
    return persist_change_risk(conn, comparison_id, assessment, params.as_json())
