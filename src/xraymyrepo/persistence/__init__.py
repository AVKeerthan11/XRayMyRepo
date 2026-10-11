"""PostgreSQL persistence: the system of record for completed CIM snapshots and the
derivations computed from them.

Requires ``psycopg`` (``pip install -e .[postgres]``) and a database migrated with
``db/migrations``. See docs/persistence/postgres-v1.md for the lifecycle,
immutability rules and the round-trip invariant.
"""

from .derivations import (
    DerivationRunExistsError,
    DerivationRunNotCompleteError,
    DerivationRunNotFoundError,
    StoredRun,
    find_derivation_run,
    load_derivation,
    persist_derivation,
)
from .snapshots import (
    PersistenceError,
    RepositoryIdentityError,
    SnapshotExistsError,
    SnapshotNotCompleteError,
    SnapshotNotFoundError,
    StoredSnapshot,
    find_snapshot,
    load_snapshot,
    persist_snapshot,
)

__all__ = [
    "DerivationRunExistsError",
    "DerivationRunNotCompleteError",
    "DerivationRunNotFoundError",
    "PersistenceError",
    "RepositoryIdentityError",
    "SnapshotExistsError",
    "SnapshotNotCompleteError",
    "SnapshotNotFoundError",
    "StoredRun",
    "StoredSnapshot",
    "find_derivation_run",
    "find_snapshot",
    "load_derivation",
    "load_snapshot",
    "persist_derivation",
    "persist_snapshot",
]
