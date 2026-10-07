"""Immutable snapshots.

A snapshot is the extracted facts of one commit, under one extractor version and
one analysis configuration. Its identity is
``(repository, commit_sha, extractor_version, config_hash)``; a snapshot never
changes to represent another commit or configuration. Re-analysis creates a new
snapshot. The only allowed change is the one-way status transition
``pending -> complete | failed``.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import Field, field_validator, model_validator

from ._base import CIMModel, GitOid, SemVer, Sha256, Slug, Text
from .enums import ExclusionAction, Origin, SnapshotStatus

CIM_SCHEMA_VERSION = "1.0"

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class RepositoryRef(CIMModel):
    host: str
    owner: str
    name: str

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        if not _HOST.fullmatch(value):
            raise ValueError(f"invalid host: {value!r}")
        return value

    @field_validator("owner", "name")
    @classmethod
    def _part(cls, value: str) -> str:
        if not _REPO_PART.fullmatch(value):
            raise ValueError(f"invalid repository owner/name: {value!r}")
        return value

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}"


DEFAULT_EXCLUSION_POLICY: dict[Origin, ExclusionAction] = {
    Origin.SOURCE: ExclusionAction.EXTRACT,
    Origin.GENERATED: ExclusionAction.EXTRACT,
    Origin.VENDORED: ExclusionAction.FILE_ONLY,
    Origin.DOCS: ExclusionAction.FILE_ONLY,
    Origin.BUILD_ARTIFACT: ExclusionAction.FILE_ONLY,
}


class AnalysisConfig(CIMModel):
    """Every input that changes extraction output, other than code and extractor version."""

    languages: tuple[Slug, ...] = Field(min_length=1)
    """Languages the extractors are enabled for, sorted."""
    exclusion_policy: dict[Origin, ExclusionAction]
    """Required, so every snapshot records its policy explicitly (see DEFAULT_EXCLUSION_POLICY)."""
    ignore_globs: tuple[Text, ...] = ()
    """Paths not recorded at all (not even as file nodes), sorted."""

    @field_validator("languages", "ignore_globs")
    @classmethod
    def _sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(set(value)):
            raise ValueError("must be sorted and unique")
        return value

    @field_validator("exclusion_policy")
    @classmethod
    def _complete(cls, value: dict[Origin, ExclusionAction]) -> dict[Origin, ExclusionAction]:
        missing = set(Origin) - set(value)
        if missing:
            raise ValueError(f"exclusion_policy must cover every origin; missing {sorted(missing)}")
        return value

    def config_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SnapshotIdentity(CIMModel):
    repository: RepositoryRef
    commit_sha: GitOid
    extractor_version: SemVer
    config_hash: Sha256

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.repository.slug, self.commit_sha, self.extractor_version, self.config_hash)


class Snapshot(CIMModel):
    identity: SnapshotIdentity
    cim_schema_version: str = CIM_SCHEMA_VERSION
    status: SnapshotStatus = SnapshotStatus.PENDING
    failure_reason: Text | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Snapshot:
        if (self.status is SnapshotStatus.FAILED) != (self.failure_reason is not None):
            raise ValueError("failure_reason is required for failed snapshots and only for them")
        return self

    def complete(self) -> Snapshot:
        self._require_pending()
        return self.model_copy(update={"status": SnapshotStatus.COMPLETE})

    def fail(self, reason: str) -> Snapshot:
        self._require_pending()
        return Snapshot(
            identity=self.identity,
            cim_schema_version=self.cim_schema_version,
            status=SnapshotStatus.FAILED,
            failure_reason=reason,
        )

    def _require_pending(self) -> None:
        if self.status is not SnapshotStatus.PENDING:
            raise ValueError(f"snapshot is {self.status}; only pending snapshots can transition")
