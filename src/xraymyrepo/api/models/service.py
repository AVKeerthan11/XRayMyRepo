"""Health and readiness."""

from __future__ import annotations

from typing import Literal

from .common import ApiModel


class Health(ApiModel):
    status: Literal["ok"]


class Readiness(ApiModel):
    status: Literal["ready", "unavailable"]
    applied_migrations: list[str]
    missing_migrations: list[str]
    detail: str | None = None
