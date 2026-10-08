"""API configuration, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    pool_min_size: int = 1
    pool_max_size: int = 10
    pool_timeout: float = 5.0
    """Seconds to wait for a database connection before answering 503."""
    cors_origins: tuple[str, ...] = ()
    """Browser origins allowed to call the API (e.g. the frontend dev server)."""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        """``XRAY_DATABASE_URL`` (required), ``XRAY_DB_POOL_MIN``, ``XRAY_DB_POOL_MAX``,
        ``XRAY_DB_POOL_TIMEOUT`` and ``XRAY_CORS_ORIGINS`` (comma-separated)."""
        url = environ.get("XRAY_DATABASE_URL")
        if not url:
            raise RuntimeError("XRAY_DATABASE_URL is not set")
        origins = environ.get("XRAY_CORS_ORIGINS", "")
        return cls(
            database_url=url,
            pool_min_size=int(environ.get("XRAY_DB_POOL_MIN", cls.pool_min_size)),
            pool_max_size=int(environ.get("XRAY_DB_POOL_MAX", cls.pool_max_size)),
            pool_timeout=float(environ.get("XRAY_DB_POOL_TIMEOUT", cls.pool_timeout)),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        )
