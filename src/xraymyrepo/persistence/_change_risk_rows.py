"""Row -> CIM change-risk record mapping, shared by the full load and the read queries.

As in ``_comparison_rows``: each ``*_COLUMNS``/``*_FROM`` pair selects one record per
row; the mapping functions read only the first columns, so queries may append their
own. Factors are grouped under their item and put in rule order, which a ``RiskItem``
requires (its first factor is ``risk.change.kind``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from xraymyrepo.cim import RiskFactorRule

ITEM_COLUMNS = "i.subject_key, i.change, i.level, i.points, i.confidence, i.caveats"
ITEM_FROM = " FROM change_risk_item i"

FACTOR_COLUMNS = (
    "f.subject_key, f.rule, f.points, f.value, f.confidence, f.message, f.evidence, f.caveats"
)
FACTOR_FROM = " FROM change_risk_factor f"

_RULE_ORDER = {rule.value: i for i, rule in enumerate(RiskFactorRule)}


def factor_data(row: Sequence[Any]) -> dict[str, Any]:
    _, rule, points, value, confidence, message, evidence, caveats = row[:8]
    return {"rule": rule, "points": points, "value": value, "confidence": confidence,
            "message": message, "evidence": evidence, "caveats": caveats}  # fmt: skip


def factors_by_subject(rows: Iterable[Sequence[Any]]) -> dict[str, list[dict[str, Any]]]:
    """Factor data per subject key, each list in rule order."""
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row[0], []).append(factor_data(row))
    for factors in found.values():
        factors.sort(key=lambda f: _RULE_ORDER[f["rule"]])
    return found


def item_data(row: Sequence[Any], factors: list[dict[str, Any]]) -> dict[str, Any]:
    subject_key, change, level, points, confidence, caveats = row[:6]
    return {"subject_key": subject_key, "change": change, "level": level, "points": points,
            "confidence": confidence, "caveats": caveats, "factors": factors}  # fmt: skip
