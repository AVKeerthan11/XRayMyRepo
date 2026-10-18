"""Change risk: how much each changed file of a comparison could break other code
(read-only).

Assessments are computed and stored outside the API
(``xraymyrepo.orchestration.assess_change_risk``); these routes only read them. Items are
listed riskiest first (level, then points, then subject key). A level is a lower bound
on what was observed; ``conclusive`` says whether anything could hide more risk.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import Field, TypeAdapter

from xraymyrepo.cim import Confidence, InvalidKeyError, NodeKind, RiskCaveat, RiskLevel
from xraymyrepo.cim.keys import validate_key
from xraymyrepo.persistence import change_risk_queries as crq
from xraymyrepo.persistence import comparison_queries as cq

from .. import cursors, mapping
from ..db import Conn
from ..deps import DEFAULT_LIMIT, CompleteChangeRisk, CompleteComparison, Cursor, Limit
from ..errors import ApiError, not_found, responses
from ..models import (
    ChangeRiskDetail,
    ChangeRiskRunSummary,
    Page,
    RiskItemDetail,
    RiskItemSummary,
)

router = APIRouter(tags=["change risk"])

RUN = "/change-risks/{run_id}"
_PROBLEMS = {"conflict": "Change risk run not complete"}

_ID_CURSOR: TypeAdapter[int] = TypeAdapter(cursors.Id)
# (level rank 0-2, points as an SQL integer, subject key): bounded like the SQL values.
_ITEM_CURSOR: TypeAdapter[crq.ItemSortKey] = TypeAdapter(
    tuple[Annotated[int, Field(ge=0, le=2)], Annotated[int, Field(ge=0, le=2**31 - 1)], cursors.Key]
)


@router.get(
    "/comparisons/{comparison_id}/change-risks",
    summary="Change-risk runs of a comparison, newest first",
    responses=responses(400, 404, 409, conflict="Comparison not complete"),
)
def list_change_risks(
    conn: Conn, comparison: CompleteComparison, limit: Limit = DEFAULT_LIMIT, cursor: Cursor = None
) -> Page[ChangeRiskRunSummary]:
    """Any status (pending and failed runs included)."""
    rows = crq.list_change_risks(
        conn, comparison.id, after=cursors.decode(cursor, _ID_CURSOR), limit=limit + 1
    )
    items, next_cursor = cursors.page(rows, limit, lambda r: r.id)
    return Page(items=[mapping.change_risk_summary(r) for r in items], next_cursor=next_cursor)


@router.get(
    RUN,
    summary="A change-risk run: its comparison, parameters, level, confidence and counts",
    responses=responses(404, 409, **_PROBLEMS),
)
def get_change_risk(conn: Conn, run: CompleteChangeRisk) -> ChangeRiskDetail:
    comparison = cq.get_comparison(conn, run.comparison_id)
    assert comparison is not None  # runs cascade with their comparison
    return ChangeRiskDetail(
        **dict(mapping.change_risk_summary(run)),
        comparison=mapping.comparison_summary(comparison),
        params=crq.change_risk_params(conn, run.id),
        counts=mapping.change_risk_counts(crq.change_risk_summary(conn, run.id)),
    )


@router.get(
    f"{RUN}/items",
    summary="Risk items, riskiest first, optionally by level, confidence and caveat",
    responses=responses(400, 404, 409, **_PROBLEMS),
)
def list_items(
    conn: Conn,
    run: CompleteChangeRisk,
    levels: Annotated[
        list[RiskLevel] | None, Query(alias="level", description="Repeatable filter")
    ] = None,
    confidences: Annotated[
        list[Confidence] | None, Query(alias="confidence", description="Repeatable filter")
    ] = None,
    caveats: Annotated[
        list[RiskCaveat] | None,
        Query(alias="caveat", description="Repeatable: items with any of these caveats"),
    ] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[RiskItemSummary]:
    """Ordered by level (high first), points (most first), then subject key. Factors come
    without their evidence; ``items/by-key`` has it."""
    rows = crq.list_items(
        conn,
        run.id,
        levels=levels or (),
        confidences=confidences or (),
        caveats=caveats or (),
        after=cursors.decode(cursor, _ITEM_CURSOR),
        limit=limit + 1,
    )
    items, next_cursor = cursors.page(rows, limit, crq.item_sort_key)
    return Page(items=[mapping.risk_item_summary(i) for i in items], next_cursor=next_cursor)


@router.get(
    f"{RUN}/items/by-key",
    summary="One risk item, with every factor's evidence",
    responses=responses(404, 409, **_PROBLEMS),
)
def get_item(
    conn: Conn,
    run: CompleteChangeRisk,
    key: Annotated[str, Query(description="A file key: the subject's key in `to`, else `from`")],
) -> RiskItemDetail:
    try:
        validate_key(NodeKind.FILE, key)
    except InvalidKeyError:
        detail = f"Parameter 'key' is not a valid CIM file key: {key!r}."
        raise ApiError(422, "invalid_key", "Invalid key", detail) from None
    item = crq.get_item(conn, run.id, key)
    if item is None:
        raise not_found("risk_item_not_found", f"No risk item {key!r} in change risk run {run.id}.")
    return mapping.risk_item_detail(item)
