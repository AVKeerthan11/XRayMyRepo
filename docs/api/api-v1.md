# API v1: read-only HTTP access to persisted snapshots

`xraymyrepo.api` is a FastAPI application over the PostgreSQL system of record. It is shaped around software
concepts (repositories, snapshots, the declaration tree, nodes, relationships, evidence), not around database
tables. It is **read-only** and will power the future frontend. The machine-readable contract is
[`openapi-v1.json`](openapi-v1.json): a test compares the app against it, so any contract change shows up as a
reviewed diff (regenerate it with `XRAY_UPDATE_OPENAPI=1 pytest tests/api/test_app.py`).

```text
repository → analyzer → SnapshotDocument → persistence → PostgreSQL → persistence.queries → api → frontend
```

## Running

```sh
pip install -e .[api]
export XRAY_DATABASE_URL=postgresql://user:pass@localhost:5432/xray   # migrated with db/migrations
uvicorn --factory xraymyrepo.api:create_app
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `XRAY_DATABASE_URL` | required | PostgreSQL connection string |
| `XRAY_DB_POOL_MIN` / `XRAY_DB_POOL_MAX` | 1 / 10 | Connection pool size |
| `XRAY_DB_POOL_TIMEOUT` | 5 | Seconds to wait for a connection before answering 503 |
| `XRAY_CORS_ORIGINS` | none | Comma-separated browser origins allowed to call the API (GET only) |

The app starts even when the database is down; `/ready` answers 503 until it is reachable and migrated.

## Layering

```text
api/         FastAPI app, routes, response models, errors, cursors, CIM → API mapping
persistence/ snapshots.py (write + full load), queries.py (focused reads), _rows.py (row ↔ CIM)
cim/         the domain contract
analyzer/    independent of persistence and the API
```

`tests/test_architecture.py` enforces the direction: `cim` imports nothing else, `analyzer` never imports
`persistence`/`api`/`psycopg`/`fastapi`, `persistence` never imports `api`, `api` never imports `analyzer`.

* **API sessions are read-only** (`default_transaction_read_only`), on top of the snapshot immutability triggers.
* **Only complete snapshots are served.** Every snapshot-scoped endpoint checks the snapshot first: unknown is
  404 `snapshot_not_found`, pending or failed is 409 `snapshot_not_complete`.
* **Response models are the API's own** (`api/models`). CIM *value types* (enums, `SourceSpan`, `Provenance`,
  evidence variants, per-kind attribute models, `AnalysisConfig`, `Producer`, `CoverageDeclaration`,
  `UnresolvedReference`, `ExtractionIssue`, `RepositoryRef`) appear inside them unchanged, so a change to one of
  them is a contract change and shows in the OpenAPI diff.

## Endpoints

All under `/api/v1`, except the two service endpoints. All are `GET`.

| Path | Purpose | Parameters | Response |
| --- | --- | --- | --- |
| `/health` | Process is alive (no database) | – | `Health` |
| `/ready` | Database reachable and migrated | – | `Readiness` (503 when not) |
| `/repositories` | List repositories | `limit`, `cursor` | `Page[Repository]` |
| `/repositories/{host}/{owner}/{name}` | One repository (owner/name ignore case) | – | `Repository` |
| `/repositories/{host}/{owner}/{name}/snapshots` | List or find snapshots, newest first | `commit_sha`, `extractor_version`, `config_hash`, `status` (default `complete`), `limit`, `cursor` | `Page[SnapshotSummary]` |
| `/snapshots/{id}` | Metadata, config, producers, coverage, root node, statistics | – | `SnapshotDetail` |
| `/snapshots/{id}/tree/children` | Expand one tree node | `parent` (key, default `/`), `kind`*, `limit`, `cursor` | `Page[NodeSummary]` |
| `/snapshots/{id}/nodes` | Nodes by kind / key prefix | `kind`*, `prefix`, `limit`, `cursor` | `Page[NodeSummary]` |
| `/snapshots/{id}/nodes/by-key` | Inspect one node | `key` | `NodeDetail` |
| `/snapshots/{id}/relationships` | Edges touching one node | `node`, `direction` (`out`/`in`/`both`), `kind`*, `limit`, `cursor` | `Page[Relationship]` |
| `/snapshots/{id}/edges/by-ref` | One edge with evidence | `kind`, `source`, `target` | `EdgeDetail` |
| `/snapshots/{id}/search` | Find nodes by name, key or path | `q`, `kind`*, `limit`, `cursor` | `Page[SearchHit]` |
| `/snapshots/{id}/diagnostics` | Unresolved references and issues around a node | `node` | `Diagnostics` |

\* repeatable (`?kind=file&kind=directory`).

**Keys are query parameters.** CIM keys contain `/`, `#`, `:`, spaces, `{}` and `@`
(`endpoint:http:GET /users/{id}@/`), so they never appear in paths. Clients must URL-encode them; in particular
`#` must be sent as `%23` (otherwise the browser treats it as a fragment). `encodeURIComponent` does this.

**Stable references** for frontend state: a node is `(snapshot, key)`; an edge is `(snapshot, kind, source_key,
target_key)`. Snapshot ids are database-generated: stable while the database lives, but not across a rebuild,
so long-lived state should also keep the snapshot identity (`repository`, `commit_sha`, `extractor_version`,
`config_hash`), which the snapshots listing can look up again.

## Lazy loading

Nothing returns a whole repository. Every list is a page of at most `limit` items (default 200, max 1000) with
an opaque `next_cursor` (keyset pagination: pages stay stable and cheap however deep you go). There are no
totals; where the UI needs a number, it is part of the item (`child_count`) or the detail
(`relationship_counts`, `diagnostic_counts`).

* **Tree.** `SnapshotDetail.root` → `tree/children?parent=<key>` per expanded node. Order: directories, then
  files (by key); inside a file, types and functions in source order, then endpoints. Each child has
  `child_count` (draw an expander without fetching) and `tags` (origin/role/layer, for colouring).
  Packages and external packages are not in the declaration tree: list them with `nodes?kind=package`.
* **Reveal.** `NodeDetail.ancestors` is the path from the root to the parent: open those nodes to show a search
  result in the tree.
* **Graph.** `relationships?node=<key>` is exactly one hop, paged: outgoing edges first, then incoming, each by
  kind and peer key. `relationship_counts` gives per-kind totals in both directions first, so the UI can show
  "2 000 incoming IMPORTS" and page through them on demand. Larger graphs are built client-side, one expansion
  at a time.
* **Direction is never inverted.** Each `Relationship` carries the stored `source_key` → `target_key`;
  `direction` only says which end the requested node is. There are no synthesized inverse kinds. A recursive
  `CALLS` self-loop appears once as `out` and once as `in`.
* **Evidence** (≤ 20 items per record, by contract) comes with `NodeDetail` (node evidence and each
  classification's evidence) and `EdgeDetail`. Span evidence gives file key and position; derived evidence gives
  node keys and edge refs to navigate to. Source text itself is not served (see Limitations).

Future graph views (neighbourhoods, subtree rollups, architecture, impact) will be new paths, for example under
`/snapshots/{id}/graph/…`; nothing above needs to change for them.

## Errors

Every error is RFC 9457 `application/problem+json`:

```json
{"type": "urn:xraymyrepo:problem:node_not_found", "title": "Not found", "status": 404,
 "detail": "No node 'src/x.py' in snapshot 3.", "code": "node_not_found",
 "instance": "/api/v1/snapshots/3/nodes/by-key"}
```

| Status | `code` |
| --- | --- |
| 404 | `repository_not_found`, `snapshot_not_found`, `node_not_found` (also for `parent`/`node`), `edge_not_found`, `not_found` (no such route) |
| 409 | `snapshot_not_complete` |
| 422 | `validation_error` (with `errors[]`: `loc`, `msg`, `type`); `invalid_key` (not a well-formed CIM key, so it can never exist) |
| 400 | `invalid_cursor` |
| 405 | `method_not_allowed` |
| 503 | `database_unavailable` |
| 500 | `internal_error` (no details disclosed; logged server-side) |

Empty results are `200` with an empty page: a leaf has no children, an unconnected node has no relationships.

## Queries and cost

Each request runs a fixed number of SQL statements, independent of page size (asserted in
`tests/api/test_api_db.py::test_query_budget`), all on the snapshot-tier indexes of migration 0001:

| Endpoint | Statements | Main indexes |
| --- | --- | --- |
| tree/children | 5 | `node_key_unique`, `node_parent_idx`, `classification_unique` |
| nodes/by-key | 7 | `node_key_unique`, `node_parent_idx`, `edge_unique`, `edge_target_idx`, … |
| relationships | 3 | `edge_unique` (out), `edge_target_idx` (in) |
| search | 4 | none for name matching: a scan of the snapshot's nodes |

## Limitations

* **Name search is not indexed.** Case-insensitive substring matching scans one snapshot's nodes; key prefixes
  use `node_key_unique`. Fine for snapshots of the expected size (the generated 8 000-node test answers in
  milliseconds); a `pg_trgm` index (migration 0003) is the planned remedy if measurements require it. Case
  folding of keys is ASCII-only (keys are `COLLATE "C"`).
* **"Latest" means most recently created complete snapshot**, not newest commit: commit dates and branches are
  not stored.
* **No source text.** Spans and `blob_sha` are exposed; serving file content needs a source/blob store
  decision.
* **No writes.** Analysis and persistence run outside the API (an `xray persist` command is a planned
  follow-up).
* A malformed `snapshot_id` is reported as 422 only when the database is reachable: the connection is acquired
  before parameters are validated, so with the database down it is 503.
