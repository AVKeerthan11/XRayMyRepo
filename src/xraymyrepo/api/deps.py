"""Request dependencies shared by the routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Query, Request

from xraymyrepo.cim import InvalidKeyError, NodeKind, SnapshotStatus
from xraymyrepo.cim.enums import SNAPSHOT_NODE_KINDS
from xraymyrepo.cim.keys import validate_key
from xraymyrepo.persistence import comparison_queries, derivation_queries, queries
from xraymyrepo.persistence.comparison_queries import ComparisonRecord
from xraymyrepo.persistence.derivation_queries import RunRecord
from xraymyrepo.persistence.queries import SnapshotRecord

from .db import Conn
from .errors import ApiError, not_found
from .graphs import GraphCache

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size")]
Cursor = Annotated[str | None, Query(description="`next_cursor` of the previous page")]


def complete_snapshot(snapshot_id: Annotated[int, Path(ge=1)], conn: Conn) -> SnapshotRecord:
    """The requested snapshot, which must be complete for its data to be served."""
    record = queries.get_snapshot(conn, snapshot_id)
    if record is None:
        raise not_found("snapshot_not_found", f"Snapshot {snapshot_id} does not exist.")
    if record.status is not SnapshotStatus.COMPLETE:
        raise ApiError(409, "snapshot_not_complete", "Snapshot not complete",
                       f"Snapshot {snapshot_id} is {record.status}.")  # fmt: skip
    return record


CompleteSnapshot = Annotated[SnapshotRecord, Depends(complete_snapshot)]


def complete_run(run_id: Annotated[int, Path(ge=1)], conn: Conn) -> RunRecord:
    """The requested derivation run, which must be complete for its data to be served.

    A complete run's snapshot is complete too: runs are only created from complete
    snapshots, and snapshots never change status afterwards.
    """
    record = derivation_queries.get_run(conn, run_id)
    if record is None:
        raise not_found("derivation_not_found", f"Derivation run {run_id} does not exist.")
    if record.status is not SnapshotStatus.COMPLETE:
        raise ApiError(409, "derivation_not_complete", "Derivation run not complete",
                       f"Derivation run {run_id} is {record.status}.")  # fmt: skip
    return record


CompleteRun = Annotated[RunRecord, Depends(complete_run)]


def complete_comparison(comparison_id: Annotated[int, Path(ge=1)], conn: Conn) -> ComparisonRecord:
    """The requested snapshot comparison, which must be complete for its changes to be
    served. Its two snapshots are complete too (comparisons are only created from
    complete snapshots)."""
    record = comparison_queries.get_comparison(conn, comparison_id)
    if record is None:
        raise not_found("comparison_not_found", f"Comparison {comparison_id} does not exist.")
    if record.status is not SnapshotStatus.COMPLETE:
        raise ApiError(409, "comparison_not_complete", "Comparison not complete",
                       f"Comparison {comparison_id} is {record.status}.")  # fmt: skip
    return record


CompleteComparison = Annotated[ComparisonRecord, Depends(complete_comparison)]


def checked_key(key: str, parameter: str) -> str:
    """Reject strings that are not a CIM node key of any snapshot kind (422).

    A malformed key can never name a node, so this is a client error, not a 404.
    """
    for kind in SNAPSHOT_NODE_KINDS:
        try:
            validate_key(kind, key)
        except InvalidKeyError:
            continue
        return key
    raise ApiError(422, "invalid_key", "Invalid key",
                   f"Parameter {parameter!r} is not a valid CIM node key: {key!r}.")  # fmt: skip


def checked_group_key(key: str, parameter: str) -> str:
    """Reject strings that are not a CIM group key (``group:<lens>:<local>``) with 422."""
    try:
        return validate_key(NodeKind.GROUP, key)
    except InvalidKeyError:
        detail = f"Parameter {parameter!r} is not a valid CIM group key: {key!r}."
        raise ApiError(422, "invalid_key", "Invalid key", detail) from None


def graph_cache(request: Request) -> GraphCache:
    cache: GraphCache = request.app.state.graph_cache
    return cache


Graphs = Annotated[GraphCache, Depends(graph_cache)]
