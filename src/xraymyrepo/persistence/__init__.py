"""PostgreSQL persistence: the system of record for completed CIM snapshots, the
derivations computed from them, the comparisons between them and the change risk of
those comparisons.

Requires ``psycopg`` (``pip install -e .[postgres]``) and a database migrated with
``db/migrations``. See docs/persistence/postgres-v1.md for the lifecycle,
immutability rules and the round-trip invariant.
"""

from .change_risk import (
    ChangeRiskExistsError,
    ChangeRiskNotCompleteError,
    ChangeRiskNotFoundError,
    find_change_risk,
    load_change_risk,
    persist_change_risk,
)
from .comparisons import (
    ComparisonExistsError,
    ComparisonNotCompleteError,
    ComparisonNotFoundError,
    find_comparison,
    load_comparison,
    load_comparison_summary,
    persist_comparison,
)
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
    "ChangeRiskExistsError",
    "ChangeRiskNotCompleteError",
    "ChangeRiskNotFoundError",
    "ComparisonExistsError",
    "ComparisonNotCompleteError",
    "ComparisonNotFoundError",
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
    "find_change_risk",
    "find_comparison",
    "find_derivation_run",
    "find_snapshot",
    "load_change_risk",
    "load_comparison",
    "load_comparison_summary",
    "load_derivation",
    "load_snapshot",
    "persist_change_risk",
    "persist_comparison",
    "persist_derivation",
    "persist_snapshot",
]
