"""Health findings: evidence-backed structural findings and observed coverage gaps.

See ``derive`` for the rules and docs/insights/insights-v1.md.
"""

from .derive import (
    RULE_BLIND_SPOT,
    RULE_CYCLE,
    RULE_FAN_OUT,
    RULE_HUB,
    RULE_UNREFERENCED,
    RULE_UNUSED,
    health,
)
from .params import ALGORITHM, ALGORITHM_VERSION, HealthParams

__all__ = [
    "ALGORITHM",
    "ALGORITHM_VERSION",
    "RULE_BLIND_SPOT",
    "RULE_CYCLE",
    "RULE_FAN_OUT",
    "RULE_HUB",
    "RULE_UNREFERENCED",
    "RULE_UNUSED",
    "HealthParams",
    "health",
]
