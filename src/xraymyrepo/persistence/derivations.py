"""Store CIM derivations (derivation tier, migration 0003) and load them back.

A ``Derivation`` is computed elsewhere (``xraymyrepo.architecture``) from one complete
snapshot; this module only maps it onto the database and back. Like snapshots:

* ``persist_derivation`` inserts the run as ``pending``, bulk-loads every row and
  marks it ``complete`` in one transaction; any failure leaves nothing behind.
* A run's identity is ``(snapshot, algorithm, algorithm_version, params_hash)``;
  persisting an identity that exists raises ``DerivationRunExistsError``.
* Complete runs and their rows are immutable (``derivation_run_guard_immutable``,
  ``derivation_child_guard_immutable``); runs are derived from complete snapshots
  only (``derivation_run_require_complete_snapshot``).
* Round trip: ``load_derivation(persist_derivation(...)) == derivation.canonical()``.

The parameters that produced the run are stored with it (``params``); they must hash
to the run's ``params_hash`` (sha256 of their canonical JSON).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

import psycopg
from pydantic import TypeAdapter

from xraymyrepo.cim import Derivation, EdgeRef, SnapshotStatus
from xraymyrepo.cim.derivation import lens_of
from xraymyrepo.cim.evidence import FactEvidence

from . import _derivation_rows as R
from . import queries
from .snapshots import (
    Connection,
    PersistenceError,
    SnapshotNotCompleteError,
    SnapshotNotFoundError,
)

_EVIDENCE: TypeAdapter[tuple[FactEvidence, ...]] = TypeAdapter(tuple[FactEvidence, ...])
_EDGE_REFS: TypeAdapter[tuple[EdgeRef, ...]] = TypeAdapter(tuple[EdgeRef, ...])
_KEYS: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])


class DerivationRunExistsError(PersistenceError):
    """A run with the same identity is stored; a run is never replaced."""

    def __init__(self, run_id: int, status: SnapshotStatus) -> None:
        super().__init__(f"derivation run {run_id} with this identity already exists ({status})")
        self.run_id = run_id
        self.status = status


class DerivationRunNotFoundError(PersistenceError):
    pass


class DerivationRunNotCompleteError(PersistenceError):
    pass


class StoredRun(NamedTuple):
    id: int
    status: SnapshotStatus


def params_hash(params: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of ``params`` (the formula of ``config_hash``)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Writing


def persist_derivation(
    conn: Connection, snapshot_id: int, derivation: Derivation, params: Mapping[str, Any]
) -> int:
    """Store ``derivation`` of snapshot ``snapshot_id`` as a complete run; returns its id.

    One transaction (a savepoint inside a caller's transaction).
    """
    run = derivation.run
    if params_hash(params) != run.params_hash:
        raise PersistenceError("params do not hash to the run's params_hash")
    with conn.transaction(), conn.cursor() as cur:
        snapshot = queries.get_snapshot(conn, snapshot_id)
        if snapshot is None:
            raise SnapshotNotFoundError(f"snapshot {snapshot_id} does not exist")
        if snapshot.status is not SnapshotStatus.COMPLETE:
            raise SnapshotNotCompleteError(f"snapshot {snapshot_id} is {snapshot.status}")
        if snapshot.identity != run.snapshot:
            raise PersistenceError(f"the derivation was not computed from snapshot {snapshot_id}")

        row = cur.execute(
            "INSERT INTO derivation_run (snapshot_id, algorithm, algorithm_version, params_hash, "
            "params) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT derivation_run_identity_key DO NOTHING RETURNING id",
            (snapshot_id, run.algorithm, run.algorithm_version, run.params_hash,
             json.dumps(params, sort_keys=True, ensure_ascii=False)),
        ).fetchone()  # fmt: skip
        if row is None:
            existing = find_derivation_run(conn, snapshot_id, run.algorithm,
                                           run.algorithm_version, run.params_hash)  # fmt: skip
            assert existing is not None
            raise DerivationRunExistsError(existing.id, existing.status)
        run_id = int(row[0])
        _write_rows(cur, run_id, snapshot_id, derivation.canonical())
        cur.execute(
            "UPDATE derivation_run SET status = 'complete', finished_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise PersistenceError(f"derivation run {run_id} is no longer pending")
    return run_id


def _write_rows(cur: psycopg.Cursor[Any], run_id: int, snapshot_id: int, d: Derivation) -> None:
    _copy(cur, "COPY lens (run_id, name, kind) FROM STDIN",
          ((run_id, lens.name, lens.kind.value) for lens in d.lenses))  # fmt: skip

    # Group ids are reserved first so parents are known before the rows exist.
    reserved = cur.execute(
        "SELECT nextval(pg_get_serial_sequence('derived_group', 'id')) FROM generate_series(1, %s)",
        (len(d.groups),),
    ).fetchall()
    group_ids = dict(zip((g.key for g in d.groups), sorted(int(r[0]) for r in reserved),
                         strict=True))  # fmt: skip
    parents = {g.key: g.parent_key for g in d.groups}
    depths: dict[str, int] = {}

    def depth(key: str) -> int:
        if key not in depths:
            parent = parents[key]
            depths[key] = 0 if parent is None else depth(parent) + 1
        return depths[key]

    _copy(
        cur,
        "COPY derived_group (run_id, id, key, lens, parent_id, depth, label, basis, confidence, "
        "producer, rule, evidence) FROM STDIN",
        ((run_id, group_ids[g.key], g.key, g.lens,
          None if g.parent_key is None else group_ids[g.parent_key], depth(g.key), g.label,
          g.basis.value, g.confidence.value, g.provenance.producer, g.provenance.rule,
          _EVIDENCE.dump_json(g.evidence).decode())
         for g in d.groups),
    )  # fmt: skip

    node_keys = sorted(
        {m.node_key for m in d.members}
        | {x.node_key for x in d.layers}
        | {m.node_key for m in d.metrics if m.node_key not in group_ids}
    )
    node_ids = {
        key: int(node_id)
        for key, node_id in cur.execute(
            "SELECT key, id FROM node WHERE snapshot_id = %s AND key = ANY(%s)",
            (snapshot_id, node_keys),
        )
    }
    missing = [k for k in node_keys if k not in node_ids]
    if missing:
        raise PersistenceError(f"the derivation cites unknown nodes, e.g. {missing[0]!r}")

    _copy(
        cur,
        "COPY group_member (run_id, lens, node_id, group_id, snapshot_id, basis, confidence) "
        "FROM STDIN",
        ((run_id, lens_of(m.group_key), node_ids[m.node_key], group_ids[m.group_key],
          snapshot_id, m.basis.value, m.confidence.value)
         for m in d.members),
    )  # fmt: skip
    _copy(
        cur,
        "COPY group_edge (run_id, lens, source_group_id, edge_kind, target_group_id, count, "
        "basis_counts, sample_edge_refs) FROM STDIN",
        ((run_id, lens_of(e.source_group_key), group_ids[e.source_group_key],
          e.edge_kind.value, group_ids[e.target_group_key], e.count,
          json.dumps({b.value: n for b, n in sorted(e.basis_counts.items())}),
          _EDGE_REFS.dump_json(e.sample_edge_refs).decode())
         for e in d.group_edges),
    )  # fmt: skip
    _copy(
        cur,
        "COPY node_metric (run_id, snapshot_id, node_id, group_id, metric, value, producer, "
        "rule) FROM STDIN",
        ((run_id, snapshot_id, node_ids.get(m.node_key), group_ids.get(m.node_key), m.metric,
          m.value, m.provenance.producer, m.provenance.rule)
         for m in d.metrics),
    )  # fmt: skip
    _copy(
        cur,
        "COPY finding (run_id, rule, severity, subject_keys, message, basis, confidence, "
        "evidence) FROM STDIN",
        ((run_id, f.rule, f.severity.value, _KEYS.dump_json(f.subject_keys).decode(),
          f.message, f.basis.value, f.confidence.value, _EVIDENCE.dump_json(f.evidence).decode())
         for f in d.findings),
    )  # fmt: skip
    _copy(
        cur,
        "COPY inferred_layer (run_id, snapshot_id, node_id, layer, basis, confidence, producer, "
        "rule, evidence) FROM STDIN",
        ((run_id, snapshot_id, node_ids[x.node_key], x.layer, x.basis.value,
          x.confidence.value, x.provenance.producer, x.provenance.rule,
          _EVIDENCE.dump_json(x.evidence).decode())
         for x in d.layers),
    )  # fmt: skip


def _copy(cur: psycopg.Cursor[Any], statement: str, rows: Iterable[tuple[Any, ...]]) -> None:
    with cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


# ---------------------------------------------------------------------------
# Reading


def find_derivation_run(
    conn: Connection, snapshot_id: int, algorithm: str, algorithm_version: str, params_hash: str
) -> StoredRun | None:
    """The stored run with this identity, in any status."""
    row = conn.execute(
        "SELECT id, status FROM derivation_run WHERE snapshot_id = %s AND algorithm = %s "
        "AND algorithm_version = %s AND params_hash = %s",
        (snapshot_id, algorithm, algorithm_version, params_hash),
    ).fetchone()
    return None if row is None else StoredRun(int(row[0]), SnapshotStatus(row[1]))


def load_derivation(conn: Connection, run_id: int) -> Derivation:
    """Load a complete run as a validated ``Derivation``, in canonical order.

    Eight queries whatever the size: the run and one per table; ids are resolved
    to group and node keys by joins.
    """
    with conn.transaction(), conn.cursor() as cur:
        header = cur.execute(
            "SELECT snapshot_id, algorithm, algorithm_version, params_hash, status "
            "FROM derivation_run WHERE id = %s",
            (run_id,),
        ).fetchone()
        if header is None:
            raise DerivationRunNotFoundError(f"derivation run {run_id} does not exist")
        snapshot_id, algorithm, version, hash_, status = header
        if status != SnapshotStatus.COMPLETE:
            raise DerivationRunNotCompleteError(f"derivation run {run_id} is {status}")
        snapshot = queries.get_snapshot(conn, snapshot_id)
        assert snapshot is not None  # the run cascades with its snapshot

        def rows(sql: str) -> list[Any]:
            return cur.execute(sql, {"run": run_id}).fetchall()

        lenses = rows("SELECT name, kind FROM lens WHERE run_id = %(run)s ORDER BY name")
        groups = rows(f"SELECT {R.GROUP_COLUMNS}{R.GROUP_FROM} WHERE g.run_id = %(run)s")
        members = rows(
            "SELECT g.key, n.key, m.basis, m.confidence FROM group_member m "
            "JOIN derived_group g ON g.run_id = m.run_id AND g.id = m.group_id "
            "JOIN node n ON n.snapshot_id = m.snapshot_id AND n.id = m.node_id "
            "WHERE m.run_id = %(run)s"
        )
        group_edges = rows(
            f"SELECT {R.GROUP_EDGE_COLUMNS}{R.GROUP_EDGE_FROM} WHERE e.run_id = %(run)s"
        )
        metrics = rows(
            "SELECT coalesce(n.key, g.key), m.metric, m.value, m.producer, m.rule "
            "FROM node_metric m "
            "LEFT JOIN node n ON n.snapshot_id = m.snapshot_id AND n.id = m.node_id "
            "LEFT JOIN derived_group g ON g.run_id = m.run_id AND g.id = m.group_id "
            "WHERE m.run_id = %(run)s"
        )
        findings = rows(f"SELECT {R.FINDING_COLUMNS}{R.FINDING_FROM} WHERE f.run_id = %(run)s")
        layers = rows(f"SELECT {R.LAYER_COLUMNS}{R.LAYER_FROM} WHERE l.run_id = %(run)s")

    derivation = Derivation.model_validate({
        "run": {"snapshot": snapshot.identity, "algorithm": algorithm,
                "algorithm_version": version, "params_hash": hash_, "status": status},
        "lenses": [{"name": n, "kind": k} for n, k in lenses],
        "groups": [R.group_data(r) for r in groups],
        "members": [{"group_key": g, "node_key": n, "basis": b, "confidence": c}
                    for g, n, b, c in members],
        "group_edges": [R.group_edge_data(r) for r in group_edges],
        "metrics": [{"node_key": key, "metric": metric, "value": value,
                     "provenance": R.provenance(producer, rule)}
                    for key, metric, value, producer, rule in metrics],
        "findings": [R.finding_data(r) for r in findings],
        "layers": [R.layer_data(r) for r in layers],
    })  # fmt: skip
    return derivation.canonical()
