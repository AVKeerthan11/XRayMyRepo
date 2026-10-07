"""Lineage: the same logical entity across two snapshots.

Keys are declared identity and change on moves and renames. Lineage records how a
node in one snapshot was matched to a node in another, and how sure we are.
Unmatched nodes are simply added or removed; no lineage row is invented for them.

v1 match methods, in the order a matcher applies them:
    same_key            identical key                        (high)
    git_rename          git reported the file renamed; the
                        symbol's qualified name is unchanged (high)
    same_content_hash   different key, identical content     (high or medium)

Fuzzy similarity matching is deliberately not part of v1.
"""

from __future__ import annotations

from pydantic import model_validator

from ._base import CIMModel
from .enums import Confidence, MatchMethod
from .snapshot import SnapshotIdentity


class NodeLineage(CIMModel):
    from_snapshot: SnapshotIdentity
    from_key: str
    to_snapshot: SnapshotIdentity
    to_key: str
    match_method: MatchMethod
    confidence: Confidence

    @model_validator(mode="after")
    def _consistent(self) -> NodeLineage:
        if self.from_snapshot.repository != self.to_snapshot.repository:
            raise ValueError("lineage links snapshots of the same repository")
        if self.from_snapshot == self.to_snapshot:
            raise ValueError("lineage links two different snapshots")
        same_key = self.from_key == self.to_key
        match self.match_method:
            case MatchMethod.SAME_KEY:
                if not same_key:
                    raise ValueError("same_key lineage requires identical keys")
                if self.confidence is not Confidence.HIGH:
                    raise ValueError("same_key lineage has high confidence")
            case MatchMethod.GIT_RENAME:
                if same_key:
                    raise ValueError("git_rename lineage requires different keys")
                if self.confidence is not Confidence.HIGH:
                    raise ValueError("git_rename lineage has high confidence")
            case MatchMethod.SAME_CONTENT_HASH:
                if same_key:
                    raise ValueError("same_content_hash lineage requires different keys")
                if self.confidence is Confidence.LOW:
                    raise ValueError("same_content_hash lineage is high or medium confidence")
        return self
