"""Snapshot comparison: what changed between two immutable snapshots of one repository.

A comparison is computed from two complete snapshots, ``from`` and ``to`` (any two:
there is no git history, so the direction is the caller's), identified by
``(from, to, algorithm, algorithm_version, params_hash)`` like a derivation run.

**Comparability.** Two snapshots are fully comparable only when they were extracted
by the same extractor version under the same configuration. Otherwise the reasons
are recorded (``incomparable``) and no change, node or edge, may claim ``high``
confidence: a difference may come from the extraction rather than from the code.

**Node changes.** One row per node of either snapshot; every key appears at most
once per side.

    change        from_key  to_key  match_method         confidence
    added         -         set     -                    high or medium
    removed       set       -       -                    high or medium
    unchanged     set       = from  same_key             high or medium
    modified      set       = from  same_key             high or medium
    moved         set       != from same_content_hash    medium
    undetermined  set       = from  same_key             low
                  one of the two    -                    low

``undetermined`` means the snapshots cannot tell (a fingerprint is missing, the
extractor versions differ, the containing file failed to parse); it is never a
guess. ``move_ambiguous`` marks an added or removed file that shared its content
with more than one candidate, so it was deliberately not matched.

**Lineage** is the existing ``NodeLineage`` contract, built from every matched row:
``same_key`` lineage is ``high`` (key equality is certain whatever the extractor; the
comparability cap applies to the *change*), ``same_content_hash`` lineage carries the
row's confidence.

**Edge changes** are compared after node matching: an edge of ``from`` is the same
edge in ``to`` when its kind is equal and both ends are matched to the ends of the
``to`` edge. Each ref is in its own snapshot's keys. Only changed edges are rows
(added, removed, modified, undetermined); unchanged edges are counted
(``unchanged_edges``). An ``undetermined`` edge (one ref, ``low``) is one the other
snapshot could not show, e.g. an import of a file that failed to parse there.
"""

from __future__ import annotations

from collections import Counter

from pydantic import Field, model_validator

from ._base import CIMModel, SemVer, Sha256, Slug
from .enums import (
    SNAPSHOT_NODE_KINDS,
    Confidence,
    EdgeChangeKind,
    EdgeKind,
    Incomparability,
    MatchMethod,
    NodeChangeKind,
    NodeKind,
    SnapshotStatus,
)
from .keys import InvalidKeyError, validate_key
from .lineage import NodeLineage
from .refs import EdgeRef
from .snapshot import SnapshotIdentity


def incomparability(
    from_snapshot: SnapshotIdentity, to_snapshot: SnapshotIdentity
) -> tuple[Incomparability, ...]:
    """Why two snapshots are not fully comparable, in enum order; empty when they are."""
    reasons = []
    if from_snapshot.extractor_version != to_snapshot.extractor_version:
        reasons.append(Incomparability.EXTRACTOR_VERSION_DIFFERS)
    if from_snapshot.config_hash != to_snapshot.config_hash:
        reasons.append(Incomparability.CONFIG_DIFFERS)
    return tuple(reasons)


class ComparisonRun(CIMModel):
    from_snapshot: SnapshotIdentity
    to_snapshot: SnapshotIdentity
    algorithm: Slug
    algorithm_version: SemVer
    params_hash: Sha256
    incomparable: tuple[Incomparability, ...]
    """Derived from the two identities (``incomparability``); recorded so it is explicit
    wherever the comparison is read."""
    status: SnapshotStatus = SnapshotStatus.PENDING

    @classmethod
    def between(
        cls,
        from_snapshot: SnapshotIdentity,
        to_snapshot: SnapshotIdentity,
        *,
        algorithm: str,
        algorithm_version: str,
        params_hash: str,
        status: SnapshotStatus = SnapshotStatus.PENDING,
    ) -> ComparisonRun:
        return cls(
            from_snapshot=from_snapshot,
            to_snapshot=to_snapshot,
            algorithm=algorithm,
            algorithm_version=algorithm_version,
            params_hash=params_hash,
            incomparable=incomparability(from_snapshot, to_snapshot),
            status=status,
        )

    @model_validator(mode="after")
    def _consistent(self) -> ComparisonRun:
        if self.from_snapshot.repository != self.to_snapshot.repository:
            raise ValueError("a comparison is between snapshots of the same repository")
        if self.from_snapshot == self.to_snapshot:
            raise ValueError("a comparison is between two different snapshots")
        if self.incomparable != incomparability(self.from_snapshot, self.to_snapshot):
            raise ValueError("incomparable must list exactly how the snapshot identities differ")
        return self

    @property
    def comparable(self) -> bool:
        return not self.incomparable


class NodeChange(CIMModel):
    change: NodeChangeKind
    kind: NodeKind
    from_key: str | None = None
    to_key: str | None = None
    match_method: MatchMethod | None = None
    confidence: Confidence
    move_ambiguous: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> NodeChange:
        if self.kind not in SNAPSHOT_NODE_KINDS:
            raise ValueError(f"'{self.kind}' nodes are not compared (not snapshot nodes)")
        for key in (self.from_key, self.to_key):
            if key is not None:
                try:
                    validate_key(self.kind, key)
                except InvalidKeyError as exc:
                    raise ValueError(str(exc)) from None
        has_from, has_to = self.from_key is not None, self.to_key is not None
        method = self.match_method
        match self.change:
            case NodeChangeKind.ADDED | NodeChangeKind.REMOVED:
                added = self.change is NodeChangeKind.ADDED
                if (has_from, has_to) != (not added, added) or method is not None:
                    side = "to_key" if added else "from_key"
                    raise ValueError(f"{self.change} nodes have only a {side} and no match_method")
            case NodeChangeKind.UNCHANGED | NodeChangeKind.MODIFIED:
                self._require_same_key()
            case NodeChangeKind.MOVED:
                if not (has_from and has_to) or self.from_key == self.to_key:
                    raise ValueError("moved nodes have two different keys")
                if method is not MatchMethod.SAME_CONTENT_HASH:
                    raise ValueError("moved nodes are matched by same_content_hash")
                if self.confidence is not Confidence.MEDIUM:
                    raise ValueError("moved nodes have medium confidence")
            case NodeChangeKind.UNDETERMINED:
                if has_from and has_to:
                    self._require_same_key()
                elif not (has_from or has_to) or method is not None:
                    raise ValueError("undetermined nodes have a key and, when only one, no match")
                if self.confidence is not Confidence.LOW:
                    raise ValueError("undetermined nodes have low confidence")
        if self.change not in (NodeChangeKind.MOVED, NodeChangeKind.UNDETERMINED) and (
            self.confidence is Confidence.LOW
        ):
            raise ValueError(f"{self.change} nodes have high or medium confidence")
        if self.move_ambiguous and not (
            self.kind is NodeKind.FILE
            and self.change in (NodeChangeKind.ADDED, NodeChangeKind.REMOVED)
        ):
            raise ValueError("move_ambiguous marks only added or removed files")
        return self

    def _require_same_key(self) -> None:
        if self.from_key is None or self.from_key != self.to_key:
            raise ValueError(f"{self.change} nodes have the same key on both sides")
        if self.match_method is not MatchMethod.SAME_KEY:
            raise ValueError(f"{self.change} nodes are matched by same_key")

    @property
    def key(self) -> str:
        """The node's key in ``to``, or in ``from`` when it is not there."""
        key = self.to_key if self.to_key is not None else self.from_key
        assert key is not None
        return key


class EdgeChange(CIMModel):
    change: EdgeChangeKind
    kind: EdgeKind
    from_ref: EdgeRef | None = None
    """The edge in ``from``, in ``from`` keys."""
    to_ref: EdgeRef | None = None
    """The edge in ``to``, in ``to`` keys."""
    confidence: Confidence

    @model_validator(mode="after")
    def _consistent(self) -> EdgeChange:
        present = (self.from_ref is not None, self.to_ref is not None)
        expected = {
            EdgeChangeKind.ADDED: ((False, True), "only a to_ref"),
            EdgeChangeKind.REMOVED: ((True, False), "only a from_ref"),
            EdgeChangeKind.MODIFIED: ((True, True), "both refs"),
        }
        if self.change is EdgeChangeKind.UNCHANGED:
            raise ValueError("unchanged edges are counted, not recorded")
        if self.change is EdgeChangeKind.UNDETERMINED:
            if present not in ((True, False), (False, True)):
                raise ValueError("undetermined edges have exactly one ref")
            if self.confidence is not Confidence.LOW:
                raise ValueError("undetermined edges have low confidence")
        else:
            refs, described = expected[self.change]
            if present != refs:
                raise ValueError(f"{self.change} edges have {described}")
            if self.confidence is Confidence.LOW:
                raise ValueError(f"{self.change} edges have high or medium confidence")
        for ref in (self.from_ref, self.to_ref):
            if ref is not None and ref.kind is not self.kind:
                raise ValueError("an edge change's refs have its kind")
        return self

    @property
    def ref(self) -> EdgeRef:
        """The edge in ``to``, or in ``from`` when it is not there."""
        ref = self.to_ref if self.to_ref is not None else self.from_ref
        assert ref is not None
        return ref


class ComparisonSummary(CIMModel):
    """Counts by change kind; every kind is present, zeros included."""

    nodes: dict[NodeChangeKind, int]
    edges: dict[EdgeChangeKind, int]


def _ref_key(ref: EdgeRef) -> tuple[str, str, str]:
    return (ref.kind.value, ref.source_key, ref.target_key)


class SnapshotComparison(CIMModel):
    """One comparison and every change it found, with cross-record checks."""

    run: ComparisonRun
    nodes: tuple[NodeChange, ...] = ()
    edges: tuple[EdgeChange, ...] = ()
    unchanged_edges: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> SnapshotComparison:
        from_keys = [n.from_key for n in self.nodes if n.from_key is not None]
        to_keys = [n.to_key for n in self.nodes if n.to_key is not None]
        for side, keys in (("from", from_keys), ("to", to_keys)):
            repeated = sorted(k for k, n in Counter(keys).items() if n > 1)
            if repeated:
                raise ValueError(f"{side} keys appear in more than one node change: {repeated}")
        from_side, to_side = set(from_keys), set(to_keys)
        # A key in both snapshots is one node, matched by key: never removed + added.
        for change in self.nodes:
            for own, other in ((change.from_key, change.to_key), (change.to_key, change.from_key)):
                if own is not None and own != other and own in from_side and own in to_side:
                    raise ValueError(f"key {own!r} is in both snapshots but not matched by key")
        from_refs = [_ref_key(e.from_ref) for e in self.edges if e.from_ref is not None]
        to_refs = [_ref_key(e.to_ref) for e in self.edges if e.to_ref is not None]
        for side, refs in (("from", from_refs), ("to", to_refs)):
            if len(set(refs)) != len(refs):
                raise ValueError(f"a {side} edge appears in more than one edge change")

        if not self.run.comparable:
            confidences = [n.confidence for n in self.nodes] + [e.confidence for e in self.edges]
            for confidence in confidences:
                if confidence is Confidence.HIGH:
                    raise ValueError(
                        f"snapshots are not fully comparable ({', '.join(self.run.incomparable)}): "
                        "no change has high confidence"
                    )

        matched = self.matched_keys()
        for edge in self.edges:
            what = f"{edge.change} edge {edge.ref.source_key!r} -> {edge.ref.target_key!r}"
            if (
                edge.from_ref is not None
                and not {
                    edge.from_ref.source_key,
                    edge.from_ref.target_key,
                }
                <= from_side
            ):
                raise ValueError(f"{what}: an end is not a node of from")
            if (
                edge.to_ref is not None
                and not {
                    edge.to_ref.source_key,
                    edge.to_ref.target_key,
                }
                <= to_side
            ):
                raise ValueError(f"{what}: an end is not a node of to")
            if (
                edge.from_ref is not None
                and edge.to_ref is not None
                and (
                    matched.get(edge.from_ref.source_key) != edge.to_ref.source_key
                    or matched.get(edge.from_ref.target_key) != edge.to_ref.target_key
                )
            ):
                raise ValueError(f"{what}: its ends are not matched to each other")

        self.lineage()  # every matched row is a valid NodeLineage
        return self

    # -- derived views -------------------------------------------------------

    def matched_keys(self) -> dict[str, str]:
        """from key -> to key of every matched node (same key or moved)."""
        return {
            n.from_key: n.to_key
            for n in self.nodes
            if n.match_method is not None and n.from_key is not None and n.to_key is not None
        }

    def lineage(self) -> tuple[NodeLineage, ...]:
        """The ``NodeLineage`` of every matched node, in from-key order."""
        found = []
        for change in sorted(self.nodes, key=lambda n: n.from_key or ""):
            if change.match_method is None or change.from_key is None or change.to_key is None:
                continue
            same_key = change.match_method is MatchMethod.SAME_KEY
            found.append(
                NodeLineage(
                    from_snapshot=self.run.from_snapshot,
                    from_key=change.from_key,
                    to_snapshot=self.run.to_snapshot,
                    to_key=change.to_key,
                    match_method=change.match_method,
                    confidence=Confidence.HIGH if same_key else change.confidence,
                )
            )
        return tuple(found)

    def summary(self) -> ComparisonSummary:
        nodes = Counter(n.change for n in self.nodes)
        edges = Counter(e.change for e in self.edges)
        edges[EdgeChangeKind.UNCHANGED] = self.unchanged_edges
        return ComparisonSummary(
            nodes={kind: nodes[kind] for kind in NodeChangeKind},
            edges={kind: edges[kind] for kind in EdgeChangeKind},
        )

    # -- canonical form ------------------------------------------------------

    def canonical(self) -> SnapshotComparison:
        """The same comparison with every collection in a deterministic order: nodes by
        key (``to`` key, else ``from`` key), edges by ref (``to``, else ``from``)."""
        return self.model_copy(
            update={
                "nodes": tuple(sorted(self.nodes, key=lambda n: (n.key, n.from_key or ""))),
                "edges": tuple(
                    sorted(
                        self.edges,
                        key=lambda e: (
                            _ref_key(e.ref),
                            _ref_key(e.from_ref) if e.from_ref is not None else ("", "", ""),
                        ),
                    )
                ),
            }
        )
