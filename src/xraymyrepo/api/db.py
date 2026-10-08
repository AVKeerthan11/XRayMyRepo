"""Database connections for request handlers.

The API only reads. Its sessions run with ``default_transaction_read_only``, so
PostgreSQL rejects any write from the API, on top of the snapshot immutability
triggers. Connections are in autocommit mode: completed snapshots never change,
so a request's statements need no shared transaction to see consistent data.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

import psycopg
from fastapi import Depends, Request
from psycopg_pool import ConnectionPool

from .settings import Settings

Connection = psycopg.Connection[Any]
Pool = ConnectionPool[Connection]


def create_pool(settings: Settings) -> Pool:
    """A closed pool; the app opens it at startup and closes it at shutdown."""
    return ConnectionPool(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=settings.pool_timeout,
        kwargs={"autocommit": True, "options": "-c default_transaction_read_only=on"},
        open=False,
        name="xray-api",
    )


def pool_of(request: Request) -> Pool:
    pool: Pool = request.app.state.pool
    return pool


def get_conn(request: Request) -> Iterator[Connection]:
    with pool_of(request).connection() as conn:
        yield conn


Conn = Annotated[Connection, Depends(get_conn)]
