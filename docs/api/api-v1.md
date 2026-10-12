# API v1: read-only HTTP access to persisted snapshots

`xraymyrepo.api` is a FastAPI application over the PostgreSQL system of record. It is shaped around software
concepts (repositories, snapshots, the declaration tree, nodes, relationships, evidence, and the derived
architecture: groups, group dependencies, layers, findings), not around database tables. It is **read-only** and will power the future frontend. The machine-readable contract is
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
| `XRAY_IMPACT_CACHE_SIZE` | 4 | Snapshot graphs kept per process for impact queries (0: no cache) |

The app starts even when the database is down; `/ready` answers 503 until it is reachable and migrated.

## Layering

```text
api/            FastAPI app, routes, response models, errors, cursors, CIM → API mapping
persistence/    snapshots.py, derivations.py (write + full load), queries.py, derivation_queries.py
                (focused reads), _rows.py, _derivation_rows.py (row ↔ CIM)
architecture/   derivation (CIM in, CIM out); orchestration.py runs load → derive → persist
cim/            the domain contract
analyzer/       independent of persistence and the API
```

`tests/test_architecture.py` enforces the direction: `cim` imports nothing else, `analyzer` never imports
`persistence`/`api`/`psycopg`/`fastapi`, `architecture` imports only `cim`, `persistence` never imports `api`
or `architecture`, and `api` imports neither `analyzer`, `architecture` nor `orchestration`. It reads
through persistence, and for on-request computations (change impact) it may run the pure `insights` and
`graph` code. It never derives-and-stores.

* **API sessions are read-only** (`default_transaction_read_only`), on top of the snapshot immutability triggers.
* **Only complete snapshots are served.** Every snapshot-scoped endpoint checks the snapshot first: unknown is
  404 `snapshot_not_found`, pending or failed is 409 `snapshot_not_complete`. Likewise every run-scoped
  endpoint checks the run: 404 `derivation_not_found`, 409 `derivation_not_complete`.
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

### Architecture intelligence

Derived conclusions have their own routes, so they can never be mistaken for snapshot facts. Concepts, rules
and confidence: docs/architecture/architecture-v1.md. `{run}` is `/derivations/{run_id}`.

| Path | Purpose | Parameters | Response |
| --- | --- | --- | --- |
| `/snapshots/{id}/derivations` | Derivation runs of a snapshot, newest first (any status) | `limit`, `cursor` | `Page[DerivationRunSummary]` |
| `{run}` | Params, lenses (group and member counts), layer coverage, finding counts per rule | – | `DerivationRunDetail` |
| `{run}/groups/children` | A lens's top-level groups, or one group's children | `lens` (required), `parent` (group key), `limit`, `cursor` | `Page[GroupSummary]` |
| `{run}/groups/by-key` | One group with provenance, evidence and ancestors | `key` | `GroupDetail` |
| `{run}/groups/members` | Files assigned directly to a group | `group`, `limit`, `cursor` | `Page[NodeSummary]` |
| `{run}/group-edges` | Rolled-up dependencies: a lens's top level, one zoom level, or one group | exactly one of `lens`, `within`, `group`; `direction` (with `group`), `kind`*, `limit`, `cursor` | `Page[GroupEdgeView]` |
| `{run}/layers` | Inferred layers by node key | `layer`, `limit`, `cursor` | `Page[LayerAssignment]` |
| `{run}/layers/by-node` | One file's layer status | `node` | `NodeLayer` |
| `{run}/layers/unclassified` | Files considered but not classified | `limit`, `cursor` | `Page[NodeBrief]` |
| `{run}/findings` | Findings in creation order: layer violations and group cycles (architecture), health findings (health) | `rule`, `subject` (node or group key), `limit`, `cursor` | `Page[FindingView]` |
| `{run}/metrics` | One metric over every subject, ranked by value (ties by subject key) | `metric` (required), `order` (`desc`/`asc`), `limit`, `cursor` | `Page[MetricView]` |

### Change impact

Computed on request from the snapshot's dependency graph, which is cached per process. Rules, confidence
and blind spots are described in docs/insights/insights-v1.md.

| Path | Purpose | Parameters | Response |
| --- | --- | --- | --- |
| `/snapshots/{id}/impact` | What is affected if these nodes change | `node`* (1–50 seed keys), `depth` (0–10, default 3), `category` (`file`/`test`/`endpoint`), `limit`, `cursor` | `ImpactReport` |
| `/snapshots/{id}/impact/blind-spots` | Files whose dependencies cannot be fully seen | `limit`, `cursor` | `Page[BlindSpotView]` |
| `/derivations/{run_id}/impact/groups` | Affected files per group of a lens, one level | `node`*, `lens` (required), `parent` (group key), `depth`, `limit`, `cursor` | `ImpactGroupReport` |

* **`ImpactReport`** contains:
  * `seeds`;
  * a `summary` over the **whole** result: counts by category, distance and confidence, plus `truncated`,
    `beyond_depth` and `blind_spot_files`;
  * one page of `items` ordered by (distance, key), each with `category`, `distance`, `confidence` and
    `via` (a shortest witness path of edge refs).
* **Paging.** Pass the same parameters with `cursor` for the next page.
* **Blind spots are counted, never added** to the items.
* **`ImpactGroupReport`** counts affected files per child group of `parent` (or per top-level group),
  including descendants, with the closest distance. It also reports `direct_files` (assigned to `parent`
  itself) and `unassigned_files` (not under `parent` in that lens).
* **Errors.** An unknown seed is 404 `node_not_found`. The repository root or more than 50 seeds is 422
  `invalid_parameters`; a malformed key is 422 `invalid_key`.

**Response semantics:**

* **Group keys** (`group:<lens>:<local>`, e.g. `group:components:pkg:pypi:acme-backend#app.users`) are query
  parameters and must be URL-encoded like node keys.
* **Inferred results say so.**
  * Groups and memberships of the `components` lens, layers and findings carry `basis: "inferred"`, a
    `confidence`, `provenance.rule` and `derived` evidence citing snapshot node keys and edge refs.
  * `directory` groups are `observed`.
  * `LensSummary.kind` says whether a whole lens is inferred.
* **`GroupSummary`** carries what a tree item needs without further requests:
  * `child_group_count` and `member_count` (direct members);
  * `metrics` (`files` including descendants, `internal_edges`, `afferent`, `efferent`, `instability`);
  * `layer_mix` (layers of the considered files in the subtree, plus `unclassified`).
* **`GroupEdgeView`** keeps the CIM rollup unchanged: `count` and `basis_counts` (never averaged), plus up to
  20 `sample_edge_refs` to open with `/snapshots/{id}/edges/by-ref`.
* **`NodeLayer.status`** has three values:
  * `classified`: with the `assignment`;
  * `unclassified`: considered, no layer could be inferred, a valid outcome;
  * `out_of_scope`: not considered, e.g. tests, generated code, other languages.
* **`LayerAssignment`** carries `node_key` only. Resolve names with the snapshot routes or `layers/by-node`.
* **`groups/children` requires `lens`,** even with `parent`. A parent of another lens is 404
  `group_not_found`; the lens is never switched silently.
* **Run detail across algorithms.** `lenses` and `layer_coverage` are produced by the architecture
  algorithm. For other runs (e.g. `health`) they are an empty list and zeros, not omitted.
* **Health runs** (docs/insights/insights-v1.md):
  * `findings` returns observed coverage gaps (`basis: "observed"`, span evidence) and inferred
    structural findings (`basis: "inferred"`);
  * `metrics?metric=fan_in` ranks files by fan-in;
  * `findings?subject=<key>` lists what is reported about one file or group.

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
* **Group trees and zoom.** `{run}/groups/children?lens=components` → expand groups with `parent=<group key>`;
  each level is in key order. `{run}/group-edges?within=<group key>` returns the dependencies between that
  group's children (one zoom level); `?lens=<lens>` those between its top-level groups; `?group=<key>` one
  group's own. The top level plus every `within` partition all group edges. `groups/members` lists a group's
  own files as node summaries, which lead back into the snapshot routes.
* **Evidence** (≤ 20 items per record, by contract) comes with `NodeDetail` (node evidence and each
  classification's evidence) and `EdgeDetail`. Span evidence gives file key and position; derived evidence gives
  node keys and edge refs to navigate to. Source text itself is not served (see Limitations).

Further views (node neighbourhoods, impact analysis) will be new paths, for example under
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
| 404 | `repository_not_found`, `snapshot_not_found`, `node_not_found` (also for `parent`/`node`), `edge_not_found`, `derivation_not_found`, `lens_not_found`, `group_not_found`, `not_found` (no such route) |
| 409 | `snapshot_not_complete`, `derivation_not_complete` |
| 422 | `validation_error` (with `errors[]`: `loc`, `msg`, `type`); `invalid_key` (not a well-formed CIM node or group key, so it can never exist; also for `findings?subject=` and impact seeds); `invalid_parameters` (`group-edges` without exactly one of `lens`/`within`/`group`, or `direction` without `group`; impact with the repository root or more than 50 seeds) |
| 400 | `invalid_cursor` |
| 405 | `method_not_allowed` |
| 503 | `database_unavailable` |
| 500 | `internal_error` (no details disclosed; logged server-side) |

Empty results are `200` with an empty page: a leaf has no children, an unconnected node has no relationships,
a project group has no direct members.

## Queries and cost

Each request runs a fixed number of SQL statements, independent of page size (asserted in
`tests/api/test_api_db.py::test_query_budget` and `tests/api/test_derivations_api_db.py::test_query_budget`),
on the indexes of migrations 0001 and 0003:

| Endpoint | Statements | Main indexes |
| --- | --- | --- |
| tree/children | 5 | `node_key_unique`, `node_parent_idx`, `classification_unique` |
| nodes/by-key | 7 | `node_key_unique`, `node_parent_idx`, `edge_unique`, `edge_target_idx`, … |
| relationships | 3 | `edge_unique` (out), `edge_target_idx` (in) |
| search | 4 | none for name matching: a scan of the snapshot's nodes |
| `{run}` | 6 | `derivation_run` primary key, `lens`, `finding_rule_idx` |
| `{run}/groups/children`, `groups/by-key` | 6 | `derived_group_key_unique`, `derived_group_parent_idx`, `group_member_group_idx`, `node_metric_group_idx` |
| `{run}/groups/members` | 5 | `group_member_group_idx`, then the snapshot node indexes |
| `{run}/group-edges` | 3 | `group_edge` primary key (out), `group_edge_target_idx` (in) |
| `{run}/layers/by-node` | 3, or 4 for a file without a layer | `inferred_layer` primary key, `node_metric_node_idx` |
| `/snapshots/{id}/impact`, `impact/blind-spots` | 1 with a cached graph, 5 when the graph is loaded | snapshot primary key; loading: `node_key_unique`, `edge` scans of one snapshot |
| `/derivations/{run}/impact/groups` | 4 with a cached graph | `group_member` primary key, `derived_group_parent_idx` |
| `{run}/layers`, `layers/unclassified`, `findings` (also with `subject`), `metrics`, `/snapshots/{id}/derivations` | 2 | `inferred_layer_layer_idx`, `finding` primary key, `derivation_run_snapshot_idx` |

## Limitations

* **Name search is not indexed.** Case-insensitive substring matching scans one snapshot's nodes; key prefixes
  use `node_key_unique`. Fine for snapshots of the expected size (the generated 8 000-node test answers in
  milliseconds); a `pg_trgm` index in a later migration is the planned remedy if measurements require it. Case
  folding of keys is ASCII-only (keys are `COLLATE "C"`).
* **"Latest" means most recently created complete snapshot**, not newest commit: commit dates and branches are
  not stored.
* **No source text.** Spans and `blob_sha` are exposed; serving file content needs a source/blob store
  decision.
* **No writes.** Analysis, persistence and derivation run outside the API: derivations through
  `xraymyrepo.orchestration.derive_architecture`. CLI commands for persisting and deriving are planned
  follow-ups.
* **Architecture results are per run.** There is no "latest derivation" shortcut: list a snapshot's runs
  and pick one (by algorithm, version and `params_hash`). Dependencies on external packages are not rolled
  up between groups (see docs/architecture/architecture-v1.md, Limitations).
* A malformed `snapshot_id` is reported as 422 only when the database is reachable: the connection is acquired
  before parameters are validated, so with the database down it is 503.
