"""Liveness and readiness (outside the versioned API)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from psycopg import Error as DatabaseError

from xraymyrepo.persistence import queries

from ..db import pool_of
from ..models import Health, Readiness

router = APIRouter(tags=["service"])


@router.get("/health", summary="The process is alive (no database call)")
def health() -> Health:
    return Health(status="ok")


@router.get(
    "/ready",
    summary="The database is reachable and migrated",
    responses={503: {"model": Readiness, "description": "Not ready"}},
)
def ready(request: Request, response: Response) -> Readiness:
    try:
        with pool_of(request).connection() as conn:
            applied = queries.applied_migrations(conn)
    except DatabaseError:  # includes PoolTimeout
        response.status_code = 503
        return Readiness(status="unavailable", applied_migrations=[],
                         missing_migrations=list(queries.REQUIRED_MIGRATIONS),
                         detail="The database is unavailable.")  # fmt: skip
    missing = [m for m in queries.REQUIRED_MIGRATIONS if m not in applied]
    if missing:
        response.status_code = 503
        return Readiness(status="unavailable", applied_migrations=applied,
                         missing_migrations=missing,
                         detail="The database schema is not migrated.")  # fmt: skip
    return Readiness(status="ready", applied_migrations=applied, missing_migrations=[])
