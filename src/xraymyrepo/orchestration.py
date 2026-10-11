"""Load -> derive -> persist: the one place that combines architecture and persistence.

``architecture`` is pure (CIM in, CIM out) and ``persistence`` knows nothing about
how derivations are computed; this module wires them for callers. No CLI in this
milestone.
"""

from __future__ import annotations

from .architecture import ALGORITHM, ALGORITHM_VERSION, ArchitectureParams, derive
from .persistence import (
    DerivationRunExistsError,
    find_derivation_run,
    load_snapshot,
    persist_derivation,
)
from .persistence.snapshots import Connection


def derive_architecture(
    conn: Connection, snapshot_id: int, params: ArchitectureParams | None = None
) -> int:
    """Derive the architecture of a stored, complete snapshot and store it; returns the
    run id.

    Raises ``SnapshotNotFoundError`` / ``SnapshotNotCompleteError`` for an unusable
    snapshot and ``DerivationRunExistsError`` when this (snapshot, algorithm version,
    params) run is already stored: derivations are deterministic, so it is never
    recomputed or replaced.
    """
    params = params or ArchitectureParams()
    existing = find_derivation_run(
        conn, snapshot_id, ALGORITHM, ALGORITHM_VERSION, params.params_hash()
    )
    if existing is not None:
        raise DerivationRunExistsError(existing.id, existing.status)
    derivation = derive(load_snapshot(conn, snapshot_id), params)
    return persist_derivation(conn, snapshot_id, derivation, params.as_json())
