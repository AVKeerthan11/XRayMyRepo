"""Architecture intelligence: the first derivation over an immutable CIM snapshot.

Groups (directory and inferred component lenses), group-to-group dependency
rollups, group metrics, inferred layers and findings, as a CIM ``Derivation``.
Depends only on ``xraymyrepo.cim``: no database, no analyzer. Deterministic: the
same snapshot, algorithm version and parameters always give the same derivation.
See docs/architecture/architecture-v1.md.
"""

from .derive import derive
from .params import ALGORITHM, ALGORITHM_VERSION, ArchitectureParams
from .validate import DerivationContractError, validate_against

__all__ = [
    "ALGORITHM",
    "ALGORITHM_VERSION",
    "ArchitectureParams",
    "DerivationContractError",
    "derive",
    "validate_against",
]
