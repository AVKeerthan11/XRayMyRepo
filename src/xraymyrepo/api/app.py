"""The FastAPI application: a read-only HTTP API over persisted CIM snapshots.

Run it with ``uvicorn --factory xraymyrepo.api:create_app`` and ``XRAY_DATABASE_URL``
set. See docs/api/api-v1.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import errors
from .db import create_pool
from .routes import catalog, nodes, relationships, service
from .settings import Settings

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(settings)
        # Do not wait for the database: the app starts, and /ready reports 503 until
        # connections succeed.
        pool.open(wait=False)
        app.state.pool = pool
        try:
            yield
        finally:
            pool.close(timeout=settings.pool_timeout)

    app = FastAPI(
        title="XRayMyRepo API",
        version="1.0.0",
        summary="Read-only access to persisted Codebase Intelligence Model snapshots.",
        lifespan=lifespan,
    )
    errors.install(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET"],
            allow_headers=["*"],
        )
    app.include_router(service.router)
    for module in (catalog, nodes, relationships):
        app.include_router(module.router, prefix=API_PREFIX)

    generate_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _without_unreferenced_schemas(generate_openapi())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app


_SCHEMA_REF = re.compile(r'"#/components/schemas/([^"]+)"')


def _without_unreferenced_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop component schemas that no path reaches.

    FastAPI registers every enum it finds in an annotation as a component, including
    ``NodeKind`` behind ``SnapshotNodeKind``, whose own schema replaces it (and which
    must not advertise the derivation-tier kind ``group``).
    """
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    reachable: set[str] = set()
    pending = set(_SCHEMA_REF.findall(json.dumps(spec.get("paths", {}))))
    while pending:
        name = pending.pop()
        if name not in reachable and name in schemas:
            reachable.add(name)
            pending |= set(_SCHEMA_REF.findall(json.dumps(schemas[name])))
    for name in set(schemas) - reachable:
        del schemas[name]
    return spec
