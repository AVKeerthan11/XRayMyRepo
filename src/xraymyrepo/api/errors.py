"""Every error is an RFC 9457 problem (``application/problem+json``) with a stable code.

404  repository_not_found, snapshot_not_found, node_not_found, edge_not_found,
     not_found (no such route)
409  snapshot_not_complete   the snapshot is pending or failed; its data is not served
422  validation_error        bad parameters (``errors`` lists them)
422  invalid_key             not a well-formed CIM node key, so it can never exist
400  invalid_cursor
503  database_unavailable
500  internal_error          nothing about the failure is disclosed
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .models import Problem

PROBLEM_MEDIA_TYPE = "application/problem+json"

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


def not_found(code: str, detail: str) -> ApiError:
    return ApiError(404, code, "Not found", detail)


def problem(
    request: Request,
    status: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body = Problem(
        type=f"urn:xraymyrepo:problem:{code}",
        title=title,
        status=status,
        detail=detail,
        code=code,
        instance=request.url.path,
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _problem_content() -> dict[str, Any]:
    return {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}}


def responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI documentation of the problems a route can answer with."""
    descriptions = {400: "Invalid cursor", 404: "Not found", 409: "Snapshot not complete",
                    422: "Invalid parameters or key", 503: "Database unavailable"}  # fmt: skip
    return {
        s: {"model": Problem, "description": descriptions[s], "content": _problem_content()}
        for s in (*statuses, 422, 503)
    }


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem(request, exc.status, exc.code, exc.title, exc.detail)

    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": e.get("type")}
            for e in exc.errors()
        ]
        return problem(request, 422, "validation_error", "Invalid request",
                       "One or more parameters are invalid.", errors)  # fmt: skip

    @app.exception_handler(HTTPException)
    def _http(request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return problem(request, exc.status_code, code, str(exc.detail), str(exc.detail))

    @app.exception_handler(psycopg.OperationalError)
    def _database(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        # Includes psycopg_pool.PoolTimeout: no connection could be obtained in time.
        log.warning("database unavailable: %s", exc)
        return problem(request, 503, "database_unavailable", "Service unavailable",
                       "The database is unavailable. Try again later.")  # fmt: skip

    @app.exception_handler(Exception)
    def _internal(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path)
        return problem(request, 500, "internal_error", "Internal server error",
                       "An unexpected error occurred.")  # fmt: skip
