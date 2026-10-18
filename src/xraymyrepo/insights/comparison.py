"""``comparison``: two snapshot documents -> ``SnapshotComparison``. Pure and deterministic.

Only what the two snapshots show is compared; nothing is guessed. Steps:

1. **Same key.** Every key present in both snapshots is one node (``same_key``):

   ============================  =====================================================
   kind                          unchanged when ...
   ============================  =====================================================
   file                          ``blob_sha`` is equal
   type / function               ``content_hash``, ``signature_hash`` and attributes are
                                 equal (see below when fingerprints cannot be compared)
   any other                     attributes are equal
   ============================  =====================================================

   Spans, evidence and provenance never count (moving code down a file is not a
   change). A key whose node kind differs (a directory became a file) is
   ``modified``, recorded with its kind in ``to``.

   Fingerprints are compared only when both snapshots were extracted by the same
   extractor version and both nodes have them. Otherwise the symbol is ``unchanged``
   when its file's blob is equal (the source is identical) and ``undetermined``
   when not.

2. **Moved files.** A removed file and an added file are one moved file
   (``same_content_hash``, ``medium``) when their blob is *eligible* and occurs exactly
   once in each whole snapshot. Eligible (Python first, ``ComparisonParams``): not the
   empty blob, a ``move_languages`` file, declaring at least ``min_move_symbols``
   type/function nodes, in both snapshots. Non-Python files and files without
   declarations are never move-matched. An eligible removed or added file whose blob
   does occur on the other side but not uniquely is left alone and marked
   ``move_ambiguous``. The declarations of a moved file are moved with it when the
   same key below the new path exists only in ``to``
   (``old.py#A.f`` -> ``new.py#A.f``, same node kind).

3. **Unmatched nodes** are ``removed`` / ``added``, except a node whose containing file
   still exists on the other side but was not extracted there (failed, unsupported,
   excluded): that is ``undetermined``.

4. **Edges**, in a shared key space: a ``from`` edge with both ends matched is looked
   up in ``to`` under the matched keys. Found: ``unchanged`` (counted) or ``modified``
   (basis or attributes differ). Not found: ``removed``; the rest of ``to`` is
   ``added``. An edge is ``undetermined`` instead when one of its ends is
   undetermined, or when its source's file exists on the other side but was not
   extracted there (its edges cannot be seen).

**Confidence.** ``high`` for decided changes; ``medium`` for moves, for edges with a
moved end, and for everything when the snapshots are not fully comparable;
``low`` for ``undetermined``. Lineage follows the ``NodeLineage`` contract
(``SnapshotComparison.lineage``).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from xraymyrepo.cim import (
    ComparisonRun,
    Confidence,
    Edge,
    EdgeChange,
    EdgeChangeKind,
    EdgeKind,
    EdgeRef,
    Incomparability,
    MatchMethod,
    NodeChange,
    NodeChangeKind,
    NodeKind,
    SnapshotComparison,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.cim._base import Slug
from xraymyrepo.cim.enums import EXTRACTED_STATUSES, SYMBOL_NODE_KINDS
from xraymyrepo.cim.keys import parse_symbol_key
from xraymyrepo.cim.nodes import Node

ALGORITHM = "comparison"
ALGORITHM_VERSION = "1.0.0"

EMPTY_BLOBS = frozenset(
    {
        "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",  # SHA-1 repositories
        "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",  # SHA-256
    }
)
_CONTAINED = (NodeKind.TYPE, NodeKind.FUNCTION, NodeKind.ENDPOINT)

EdgeKey = tuple[EdgeKind, str, str]


class ComparisonParams(BaseModel):
    """Every input of a comparison besides the two snapshots and the code version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    move_languages: tuple[Slug, ...] = ("python",)
    """Languages whose files can be move-matched."""
    min_move_symbols: int = Field(default=1, ge=1)
    """Declarations (type/function nodes) a file needs to be move-matched: content
    without any declaration is too trivial to identify a file by."""

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = self.model_dump(mode="json")
        return data

    def params_hash(self) -> str:
        canonical = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ComparisonContractError(ValueError):
    """A comparison that does not fit its two snapshots: a bug, never repaired."""


# ---------------------------------------------------------------------------
# One side


class _Side:
    """Lookups over one snapshot document."""

    def __init__(self, doc: SnapshotDocument) -> None:
        self.doc = doc
        self.nodes = doc.nodes_by_key
        self.edges = {(e.kind, e.source_key, e.target_key): e for e in doc.edges}
        self.blob_count = Counter(n.blob_sha for n in doc.nodes if n.kind is NodeKind.FILE)
        self.declared: dict[str, list[str]] = {}
        """file key -> keys of the type/function nodes declared in it, in key order."""
        for node in doc.nodes:
            if node.kind in SYMBOL_NODE_KINDS:
                file_key = self.file_of(node.key)
                if file_key is not None:
                    self.declared.setdefault(file_key, []).append(node.key)

    def file_of(self, key: str) -> str | None:
        node = self.nodes[key]
        while node.kind in _CONTAINED:
            assert node.parent_key is not None
            node = self.nodes[node.parent_key]
        return node.key if node.kind is NodeKind.FILE else None

    def extracted(self, file_key: str) -> bool:
        return self.nodes[file_key].extraction_status in EXTRACTED_STATUSES


# ---------------------------------------------------------------------------
# The algorithm


class _Comparison:
    def __init__(
        self, before: SnapshotDocument, after: SnapshotDocument, params: ComparisonParams
    ) -> None:
        self.a = _Side(before.canonical())
        self.b = _Side(after.canonical())
        self.p = params
        self.run = ComparisonRun.between(
            before.snapshot,
            after.snapshot,
            algorithm=ALGORITHM,
            algorithm_version=ALGORITHM_VERSION,
            params_hash=params.params_hash(),
            status=SnapshotStatus.COMPLETE,
        )
        self.same_extractor = Incomparability.EXTRACTOR_VERSION_DIFFERS not in self.run.incomparable
        self.rows: dict[tuple[str | None, str | None], NodeChange] = {}

    def cap(self, confidence: Confidence) -> Confidence:
        if confidence is Confidence.HIGH and not self.run.comparable:
            return Confidence.MEDIUM
        return confidence

    def add(self, change: NodeChange) -> None:
        self.rows[(change.from_key, change.to_key)] = change

    # -- nodes ---------------------------------------------------------------

    def nodes(self) -> None:
        a, b = self.a, self.b
        for key in sorted(a.nodes.keys() & b.nodes.keys()):
            self.add(self.same_key(a.nodes[key], b.nodes[key]))
        removed = sorted(a.nodes.keys() - b.nodes.keys())
        added = sorted(b.nodes.keys() - a.nodes.keys())
        moved_from, moved_to = self.moves(removed, added)
        ambiguous = self.ambiguous(removed, added)
        for key in removed:
            if key not in moved_from:
                self.add(self.unmatched(key, self.a, self.b, ambiguous))
        for key in added:
            if key not in moved_to:
                self.add(self.unmatched(key, self.b, self.a, ambiguous))

    def same_key(self, before: Node, after: Node) -> NodeChange:
        def row(change: NodeChangeKind) -> NodeChange:
            undetermined = change is NodeChangeKind.UNDETERMINED
            return NodeChange(
                change=change,
                kind=after.kind,
                from_key=before.key,
                to_key=after.key,
                match_method=MatchMethod.SAME_KEY,
                confidence=Confidence.LOW if undetermined else self.cap(Confidence.HIGH),
            )

        if before.kind is not after.kind:
            return row(NodeChangeKind.MODIFIED)
        if before.kind is NodeKind.FILE:
            same = before.blob_sha == after.blob_sha
        elif before.kind in SYMBOL_NODE_KINDS:
            fingerprints = (
                before.content_hash,
                before.signature_hash,
                after.content_hash,
                after.signature_hash,
            )
            if self.same_extractor and None not in fingerprints:
                same = (
                    before.content_hash == after.content_hash
                    and before.signature_hash == after.signature_hash
                    and before.attributes == after.attributes
                )
            else:
                file_before, file_after = self.a.file_of(before.key), self.b.file_of(after.key)
                if file_before is None or file_before != file_after:
                    return row(NodeChangeKind.UNDETERMINED)
                if self.a.nodes[file_before].blob_sha != self.b.nodes[file_after].blob_sha:
                    return row(NodeChangeKind.UNDETERMINED)
                same = True
        else:
            same = before.attributes == after.attributes
        return row(NodeChangeKind.UNCHANGED if same else NodeChangeKind.MODIFIED)

    def eligible(self, side: _Side, key: str) -> bool:
        node = side.nodes[key]
        return (
            node.kind is NodeKind.FILE
            and node.blob_sha not in EMPTY_BLOBS
            and node.language in self.p.move_languages
            and len(side.declared.get(key, ())) >= self.p.min_move_symbols
        )

    def moves(self, removed: list[str], added: list[str]) -> tuple[set[str], set[str]]:
        """Moved files and their declarations; returns the matched from / to keys."""
        added_by_blob = {self.b.nodes[k].blob_sha: k for k in added if self.eligible(self.b, k)}
        moved_from: set[str] = set()
        moved_to: set[str] = set()
        added_set = set(added)
        for key in removed:
            if not self.eligible(self.a, key):
                continue
            blob = self.a.nodes[key].blob_sha
            target = added_by_blob.get(blob)
            if target is None or self.a.blob_count[blob] != 1 or self.b.blob_count[blob] != 1:
                continue
            pairs = [(key, target)]
            for inner in self.a.declared.get(key, ()):
                moved_key = target + inner[len(key) :]  # same key below the new path
                if (
                    moved_key in added_set
                    and self.b.nodes[moved_key].kind is self.a.nodes[inner].kind
                ):
                    pairs.append((inner, moved_key))
            for old, new in pairs:
                self.add(
                    NodeChange(
                        change=NodeChangeKind.MOVED,
                        kind=self.b.nodes[new].kind,
                        from_key=old,
                        to_key=new,
                        match_method=MatchMethod.SAME_CONTENT_HASH,
                        confidence=Confidence.MEDIUM,
                    )
                )
                moved_from.add(old)
                moved_to.add(new)
        return moved_from, moved_to

    def ambiguous(self, removed: list[str], added: list[str]) -> set[str]:
        """Eligible unmatched files whose content is on the other side, not uniquely."""
        found = set()
        for keys, side, other in ((removed, self.a, self.b), (added, self.b, self.a)):
            for key in keys:
                if not self.eligible(side, key):
                    continue
                blob = side.nodes[key].blob_sha
                on_other_side = other.blob_count[blob]
                if on_other_side and (side.blob_count[blob] > 1 or on_other_side > 1):
                    found.add(key)
        return found

    def unmatched(self, key: str, side: _Side, other: _Side, ambiguous: set[str]) -> NodeChange:
        node = side.nodes[key]
        removed = side is self.a
        keys = {"from_key": key} if removed else {"to_key": key}
        file_key = side.file_of(key)
        if (
            file_key is not None
            and file_key != key
            and file_key in other.nodes
            and not other.extracted(file_key)
        ):
            return NodeChange(
                change=NodeChangeKind.UNDETERMINED,
                kind=node.kind,
                confidence=Confidence.LOW,
                **keys,
            )
        return NodeChange(
            change=NodeChangeKind.REMOVED if removed else NodeChangeKind.ADDED,
            kind=node.kind,
            confidence=self.cap(Confidence.HIGH),
            move_ambiguous=key in ambiguous,
            **keys,
        )

    # -- edges ---------------------------------------------------------------

    def edges(self) -> tuple[list[EdgeChange], int]:
        rows = list(self.rows.values())
        forward = {r.from_key: r.to_key for r in rows if r.match_method is not None}
        backward = {to: fr for fr, to in forward.items()}
        change_of = {r.from_key: r for r in rows if r.from_key is not None}
        change_of_to = {r.to_key: r for r in rows if r.to_key is not None}
        found: list[EdgeChange] = []
        unchanged = 0
        seen: set[EdgeKey] = set()

        def emit(
            change: EdgeChangeKind,
            edge: Edge,
            ends: tuple[NodeChange, NodeChange],
            *,
            from_ref: EdgeRef | None = None,
            to_ref: EdgeRef | None = None,
        ) -> None:
            if change is EdgeChangeKind.UNDETERMINED:
                confidence = Confidence.LOW
            elif any(e.change is NodeChangeKind.MOVED for e in ends):
                confidence = Confidence.MEDIUM
            else:
                confidence = self.cap(Confidence.HIGH)
            found.append(
                EdgeChange(
                    change=change,
                    kind=edge.kind,
                    from_ref=from_ref,
                    to_ref=to_ref,
                    confidence=confidence,
                )
            )

        def hidden(
            edge: Edge,
            own: _Side,
            other: _Side,
            ends: tuple[NodeChange, NodeChange],
            counterpart: Callable[[str], str | None],
        ) -> bool:
            """An unmatched edge one snapshot could not show: an end is undetermined, or
            an end's file was extracted on one side only (its edges, or how imports of
            it resolve, depend on the extraction)."""
            if any(e.change is NodeChangeKind.UNDETERMINED for e in ends):
                return True
            for end in (edge.source_key, edge.target_key):
                file_key = own.file_of(end)
                there = None if file_key is None else counterpart(file_key)
                if (
                    file_key is not None
                    and there is not None
                    and there in other.nodes
                    and own.extracted(file_key) != other.extracted(there)
                ):
                    return True
            return False

        for _, edge in sorted(self.a.edges.items()):
            ends = (change_of[edge.source_key], change_of[edge.target_key])
            source, target = forward.get(edge.source_key), forward.get(edge.target_key)
            match = None
            if source is not None and target is not None:
                match = self.b.edges.get((edge.kind, source, target))
            if match is not None:
                seen.add((match.kind, match.source_key, match.target_key))
                if (edge.basis, edge.attributes) == (match.basis, match.attributes):
                    unchanged += 1
                else:
                    emit(
                        EdgeChangeKind.MODIFIED, edge, ends, from_ref=_ref(edge), to_ref=_ref(match)
                    )
            elif hidden(edge, self.a, self.b, ends, forward.get):
                emit(EdgeChangeKind.UNDETERMINED, edge, ends, from_ref=_ref(edge))
            else:
                emit(EdgeChangeKind.REMOVED, edge, ends, from_ref=_ref(edge))
        for key, edge in sorted(self.b.edges.items()):
            if key in seen:
                continue
            ends = (change_of_to[edge.source_key], change_of_to[edge.target_key])
            if hidden(edge, self.b, self.a, ends, backward.get):
                emit(EdgeChangeKind.UNDETERMINED, edge, ends, to_ref=_ref(edge))
            else:
                emit(EdgeChangeKind.ADDED, edge, ends, to_ref=_ref(edge))
        return found, unchanged


def _ref(edge: Edge) -> EdgeRef:
    return EdgeRef.of(edge.kind, edge.source_key, edge.target_key)


def compare(
    before: SnapshotDocument, after: SnapshotDocument, params: ComparisonParams | None = None
) -> SnapshotComparison:
    """Compare two snapshot documents of one repository (``before`` -> ``after``).
    Returned in canonical order with status ``complete``, checked against both."""
    params = params or ComparisonParams()
    c = _Comparison(before, after, params)
    c.nodes()
    edges, unchanged = c.edges()
    try:
        comparison = SnapshotComparison(
            run=c.run, nodes=tuple(c.rows.values()), edges=tuple(edges), unchanged_edges=unchanged
        ).canonical()
    except ValidationError as exc:
        raise ComparisonContractError(str(exc)) from exc
    validate_against(before, after, comparison)
    return comparison


# ---------------------------------------------------------------------------
# Checks against the two snapshots


def validate_against(
    before: SnapshotDocument, after: SnapshotDocument, comparison: SnapshotComparison
) -> None:
    """What ``SnapshotComparison`` cannot check without the snapshots: the run names
    them; every node of each is in exactly one row and every row names existing nodes
    of the right kind; moved files have equal blobs and moved declarations move with
    their file; every edge ref exists on its side, and every edge of each snapshot is
    accounted for exactly once (rows plus ``unchanged_edges``)."""

    def fail(message: str) -> NoReturn:
        raise ComparisonContractError(message)

    run = comparison.run
    if run.from_snapshot != before.snapshot or run.to_snapshot != after.snapshot:
        fail("the comparison run names other snapshots")
    a, b = before.nodes_by_key, after.nodes_by_key
    for side, keys, nodes in (
        ("from", {n.from_key for n in comparison.nodes if n.from_key is not None}, a),
        ("to", {n.to_key for n in comparison.nodes if n.to_key is not None}, b),
    ):
        if keys != nodes.keys():
            wrong = sorted(keys ^ nodes.keys())[:5]
            fail(f"node changes do not cover the {side} snapshot exactly: {wrong}")

    moved = comparison.matched_keys()
    for change in comparison.nodes:
        before_node = None if change.from_key is None else a[change.from_key]
        after_node = None if change.to_key is None else b[change.to_key]
        own = after_node or before_node
        assert own is not None
        if own.kind is not change.kind:
            fail(f"{change.key!r} is a {own.kind}, not a {change.kind}")
        if (
            before_node is not None
            and after_node is not None
            and before_node.kind is not after_node.kind
            and change.change is not NodeChangeKind.MODIFIED
        ):
            fail(f"{change.key!r} changed kind but is {change.change}")
        if change.change is NodeChangeKind.MOVED:
            assert before_node is not None and after_node is not None
            if change.kind is NodeKind.FILE:
                if before_node.blob_sha != after_node.blob_sha:
                    fail(f"moved file {change.from_key!r} -> {change.to_key!r} changed content")
            else:
                file_before = parse_symbol_key(before_node.key).file_key
                file_after = parse_symbol_key(after_node.key).file_key
                if moved.get(file_before) != file_after:
                    fail(f"moved {change.from_key!r} did not move with its file")

    before_edges = {(e.kind, e.source_key, e.target_key) for e in before.edges}
    after_edges = {(e.kind, e.source_key, e.target_key) for e in after.edges}
    for edge_change in comparison.edges:
        sides = (
            (edge_change.from_ref, before_edges, "from"),
            (edge_change.to_ref, after_edges, "to"),
        )
        for ref, edges, side in sides:
            if ref is not None and (ref.kind, ref.source_key, ref.target_key) not in edges:
                what = f"{edge_change.change} edge {ref.source_key!r} -> {ref.target_key!r}"
                fail(f"{what} is not an edge of {side}")
    from_rows = sum(1 for e in comparison.edges if e.from_ref is not None)
    to_rows = sum(1 for e in comparison.edges if e.to_ref is not None)
    if from_rows + comparison.unchanged_edges != len(before_edges):
        fail("edge changes do not account for every edge of from")
    if to_rows + comparison.unchanged_edges != len(after_edges):
        fail("edge changes do not account for every edge of to")
