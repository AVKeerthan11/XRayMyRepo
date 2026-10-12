"""Load -> derive -> persist: the one place that combines the derivation algorithms with
persistence.

``architecture`` and ``insights`` are pure (CIM in, CIM out) and ``persistence``
knows nothing about how derivations are computed; this module wires them for callers.
No CLI in this milestone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from xraymyrepo.cim import Derivation, SnapshotDocument

from . import architecture
from .architecture import ArchitectureParams, derive
from .insights import health as health_algorithm
from .insights.health import HealthParams, health
from .persistence import (
    DerivationRunExistsError,
    find_derivation_run,
    load_snapshot,
    persist_derivation,
)
from .persistence.snapshots import Connection


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
