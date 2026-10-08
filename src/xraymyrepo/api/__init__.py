"""XRayMyRepo HTTP API v1: read-only access to persisted CIM snapshots.

Layering: ``api`` -> ``persistence`` -> ``cim``. The API never writes; the
analyzer and persistence never import it. Requires ``pip install -e .[api]``.
See docs/api/api-v1.md.
"""

from .app import API_PREFIX, create_app
from .settings import Settings

__all__ = ["API_PREFIX", "Settings", "create_app"]
