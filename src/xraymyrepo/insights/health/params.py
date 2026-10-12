"""Every input of a health derivation besides the snapshot and the code version.

Any change here changes ``params_hash`` and therefore the run's identity.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from xraymyrepo.cim import EdgeKind, Origin, RequirementScope
from xraymyrepo.cim._base import Slug

ALGORITHM = "health"
ALGORITHM_VERSION = "1.0.0"


class HealthParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dependency_kinds: tuple[EdgeKind, ...] = (EdgeKind.CALLS, EdgeKind.IMPORTS, EdgeKind.INHERITS)
    """Edge kinds that mean "source depends on target"."""
    languages: tuple[Slug, ...] = ("python",)
    origins: tuple[Origin, ...] = (Origin.SOURCE,)
    """Origins of the files the structural rules consider."""
    test_directories: tuple[str, ...] = ("test", "tests")
    test_file_patterns: tuple[str, ...] = ("*_test.py", "conftest.py", "test_*.py")

    min_fan_in: int = Field(default=20, ge=1)
    min_fan_out: int = Field(default=20, ge=1)
    top_percent: float = Field(default=5.0, gt=0, le=100)
    """A hub must also be in this top share of considered files by fan-in (fan-out)."""

    root_module_names: tuple[str, ...] = (
        "__init__.py",
        "__main__.py",
        "app.py",
        "asgi.py",
        "main.py",
        "manage.py",
        "setup.py",
        "wsgi.py",
    )
    """File names that are entry points or package roots: never reported as unreferenced."""
    unused_declared_scopes: tuple[RequirementScope, ...] = (
        RequirementScope.OPTIONAL,
        RequirementScope.PROD,
    )
    """Requirement scopes checked by ``health.dependency.unused_declared`` (not ``dev``:
    tools such as test runners are rarely imported)."""
    evidence_sample: int = Field(default=20, ge=1, le=20)

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = self.model_dump(mode="json")
        return data

    def params_hash(self) -> str:
        canonical = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
