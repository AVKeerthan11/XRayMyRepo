# Persistence v1: PostgreSQL snapshot store

PostgreSQL is the system of record for completed CIM snapshots. `xraymyrepo.persistence` stores a validated
`SnapshotDocument` in the snapshot-tier schema (`db/migrations`) and loads it back. It does not change the CIM:
no new kinds, fields or tables. It uses psycopg 3 and plain parameterized SQL, with no ORM.

```text
repository → analyze() → SnapshotDocument → persist_snapshot() → PostgreSQL → load_snapshot() → SnapshotDocument
```

```python
import psycopg
from xraymyrepo.persistence import find_snapshot, load_snapshot, persist_snapshot

with psycopg.connect(url, autocommit=True) as conn:
    snapshot_id = persist_snapshot(conn, doc)
    same = load_snapshot(conn, snapshot_id)  # == doc.canonical()
    find_snapshot(conn, doc.snapshot)  # StoredSnapshot(id, status) | None
```

Install with `pip install -e .[postgres]`. The database must have every migration applied, in order.

## API

| Function | Does |
| --- | --- |
| `persist_snapshot(conn, doc) -> int` | Stores `doc` as a **complete** snapshot in one transaction and returns its id. |
| `load_snapshot(conn, snapshot_id) -> SnapshotDocument` | Loads a complete snapshot as a validated document, in canonical order. |
| `find_snapshot(conn, identity) -> StoredSnapshot \| None` | Looks up a snapshot (any status) by its exact identity. |

Errors (all `PersistenceError`): `SnapshotExistsError` (identity already stored; carries `snapshot_id` and
`status`), `RepositoryIdentityError`, `SnapshotNotFoundError`, `SnapshotNotCompleteError`. Database
constraint violations propagate as psycopg errors.

### Read queries (`xraymyrepo.persistence.queries`)

For callers that need part of a snapshot (the API), `queries` answers focused questions with a bounded number
of indexed statements instead of materializing the document: repositories and snapshots (`list_repositories`,
`get_repository`, `list_snapshots`, `get_snapshot`, `get_snapshot_setup`, `snapshot_stats`), nodes (`get_node`,
`get_node_brief`, `get_children`, `count_children`, `get_ancestors`, `list_nodes`, `search_nodes`, `get_tags`,
`get_classifications`), edges (`get_edges`, `edge_counts`, `get_edge`) and diagnostics (`get_diagnostics`,
`diagnostic_counts`). They take and return CIM keys and records, never database ids; paged functions use
keyset pagination (`after` = the sort key of the last item, see the `*_sort_key` helpers). They assume the
snapshot is complete. Row-to-record mapping (`_rows.py`) is shared with `load_snapshot`.
`REQUIRED_MIGRATIONS` lists the migrations the code depends on.

## Flow and transaction lifecycle

`persist_snapshot` runs in **one transaction**:

1. Upsert `repository` and the snapshot's `producer` rows (both are shared across snapshots).
2. `INSERT snapshot` with status `pending` (`ON CONFLICT ON CONSTRAINT snapshot_identity_key DO NOTHING`; a
   conflict raises `SnapshotExistsError`).
3. `COPY snapshot_producer`, `node`, `edge`, `classification`, `unresolved_reference`, `extraction_issue`:
   one `COPY` statement per table.
4. `UPDATE snapshot SET status = 'complete', finished_at = now()`. This is the one-way transition that
   `snapshot_guard_immutable` permits.
5. Commit.

Any exception, from Python or the database, rolls back the whole transaction. No snapshot row, data rows,
repository row or producer row from that attempt remains, so other sessions never see a `pending` or
half-loaded snapshot. The identity is then free and the snapshot can simply be persisted again. If the caller
already has a transaction open, psycopg uses a savepoint and the snapshot commits (or rolls back) with the
caller's transaction.

`failed` is not written by persistence. A persistence failure is not an extraction result, and a `failed` row
would permanently occupy the snapshot identity. The status stays available for a future extraction worker
that claims a snapshot before analyzing it.

### Bulk loading

* **Nodes.** Ids are reserved first from the identity sequence, in one query
  (`nextval(pg_get_serial_sequence('node', 'id'))` × n). That way `parent_id` and `file_id` are known in
  Python, and all nodes go in with a single `COPY`. `COPY` writes identity values as given, and foreign keys
  are checked at the end of the statement, so row order does not matter. Ids follow node-key order.
* **Derived columns.** `depth` and `file_id` are computed from `parent_key`. They are not part of the
  document.
* **Other rows.** Edges, classifications, unresolved references and issues map node keys to ids in memory
  and use one `COPY` each.
* **JSON.** Evidence, attributes, config, coverage and candidates are serialized once, by pydantic, straight
  to JSON text.

## Round-trip invariant

```text
load_snapshot(conn, persist_snapshot(conn, doc)) == doc.canonical()
```

* **What is preserved.** Every field round-trips: node keys, kinds, names, attributes, parent keys,
  language, spans, basis, confidence, provenance (producer name and rule), fingerprints, file columns;
  edges with their attributes, occurrence counts, ambiguity groups and contested flags; classifications;
  unresolved references with candidates; extraction issues; producers, config, coverage and identity.
* **Evidence.** It is stored verbatim as JSONB, so item order and every field are preserved.
* **Collection order.** It is not stored. A snapshot's collections are sets, and `load_snapshot` returns
  `canonical()` order. Loading the same snapshot twice gives equal documents.
* **Validation on load.** `load_snapshot` builds the result through `SnapshotDocument.model_validate`, so
  every CIM invariant is re-checked on load. Persistence does not duplicate those checks.
* **Bounded queries.** Loading uses seven queries regardless of snapshot size: header, producers, nodes,
  edges, classifications, unresolved references and issues. There are no N+1 lookups.
* **JSON object key order.** JSONB does not keep object key order. Model fields come back in declaration
  order anyway, because the document is rebuilt through the models. The one `dict` in the CIM,
  `AnalysisConfig.exclusion_policy`, is put in `Origin` declaration order by `SnapshotDocument.canonical()`,
  so the reloaded canonical JSON is byte-identical whatever order the original was built in.

Not part of the document, and so not returned: `snapshot.created_at`/`finished_at`, `repository.created_at`,
and database ids.

## Immutability

A complete snapshot never changes. Two triggers enforce this, both raising SQLSTATE `23000`:

* **The snapshot row:** `snapshot_guard_immutable` (migration 0001). Identity, config and coverage never
  change, and only `pending` snapshots can transition.
* **The snapshot's rows:** `snapshot_child_guard_immutable` (migration 0002). It rejects any `INSERT`,
  `UPDATE` or `DELETE` on `snapshot_producer`, `node`, `edge`, `classification`, `unresolved_reference` or
  `extraction_issue` rows of a snapshot that is not `pending`.
  * The triggers are statement-level over transition tables, so a bulk `COPY` into a pending snapshot costs
    one join per statement, not one lookup per row.
  * This extends the 0001 mechanism to the rows the snapshot owns; it is not a second system.

The persistence API has no update path. Persisting an identity that already exists raises
`SnapshotExistsError` and changes nothing.

Deleting a whole snapshot (`DELETE FROM snapshot`) still cascades, as 0001 declares. That removes the
snapshot as a unit (retention); it is not a change to what a kept snapshot says.

## Decisions (2026-10-08)

1. **One transaction.** Pending, loading and complete all happen in one transaction; nothing is left behind
   on failure, and `failed` is not used by persistence.
2. **Migration 0002** adds the row-level immutability guard. 0001 and the CIM are unchanged.
3. **Repeated persistence.** The same identity (repository, commit, extractor version, config hash) is
   stored once, and a second `persist_snapshot` raises `SnapshotExistsError`. A new extractor version or
   config is a new snapshot; repository and producer rows are shared.
4. **Repository spelling.** `repository` is unique case-insensitively on owner and name. A document whose
   owner or name differs only in case from the stored row is rejected (`RepositoryIdentityError`) rather
   than silently stored under the other spelling, which would break the round trip.

## Limitations

* **Memory.** Loading materializes the whole snapshot in memory, which is also what `SnapshotDocument`
  requires. There is no partial or streaming load.
* **No listing or deletion API.** The API only persists, finds and loads snapshots.
* **Schema version.** Snapshots are loaded with the current CIM models. A snapshot stored under another
  `cim_schema_version` fails validation on load; no migration of stored documents exists.
* **Re-running migrations.** Migrations are applied by the caller (the tests apply all of
  `db/migrations/*.sql` in order). There is no migration runner.
