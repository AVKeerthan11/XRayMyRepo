"""Request dependencies shared by the routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Query

from xraymyrepo.cim import InvalidKeyError, SnapshotStatus
from xraymyrepo.cim.enums import SNAPSHOT_NODE_KINDS
from xraymyrepo.cim.keys import validate_key
from xraymyrepo.persistence import queries
from xraymyrepo.persistence.queries import SnapshotRecord

from .db import Conn
from .errors import ApiError, not_found

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
