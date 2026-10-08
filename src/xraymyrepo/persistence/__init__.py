"""PostgreSQL persistence: the system of record for completed CIM snapshots.

Requires ``psycopg`` (``pip install -e .[postgres]``) and a database migrated with
``db/migrations``. See docs/persistence/postgres-v1.md for the lifecycle,
immutability rules and the round-trip invariant.
"""

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
    "PersistenceError",
    "RepositoryIdentityError",
    "SnapshotExistsError",
    "SnapshotNotCompleteError",
    "SnapshotNotFoundError",
    "StoredSnapshot",
    "find_snapshot",
    "load_snapshot",
    "persist_snapshot",
]
