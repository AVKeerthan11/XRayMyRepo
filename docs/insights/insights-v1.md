# Insights v1: health findings and change impact

`xraymyrepo.insights` derives evidence-backed insights from immutable CIM snapshots. Like
`xraymyrepo.architecture`, it is pure: snapshot in, CIM `Derivation` out, deterministic. It imports only
`xraymyrepo.cim` and the shared `xraymyrepo.graph`.

Milestone 6 adds it in phases: **health findings** (phase 1) and **change impact** (phase 2) are described
here; snapshot comparison and change risk follow.

## Health (`algorithm = "health"`, version `1.0.0`)

`health(doc, params=None) -> Derivation` produces **findings and metrics only**: no lenses or groups. It does
not repeat what the architecture derivation reports:

| Architecture derivation reports | Health reports |
| --- | --- |
| cycles between *components* (`arch.group.cycle`) | cycles between *files* (`health.import.cycle`) |
| layer violations | hubs, fan-out, unreferenced modules, unused declared requirements |
| group metrics (`files`, `afferent`, `instability`, …) | file metrics (`fan_in`, `fan_out`) |
| — | coverage blind spots |

### Two kinds of findings

**Observed coverage gaps** restate what the extractors could not see. They are never inferences:

| Rule | Fires for | Basis / confidence | Evidence |
| --- | --- | --- | --- |
| `health.coverage.blind_spot` | a considered-language source file whose extraction `failed` or was `partial`, or that has unresolved references (any reason) | `observed` / `high` | the **source spans** of its extraction issues and unresolved references (span evidence, basis `observed`) |

**Inferred structural findings** are computed over the dependency graph (`IMPORTS`, `INHERITS`, `CALLS` by
default):

| Rule | Fires when | Confidence | Evidence | Severity |
| --- | --- | --- | --- | --- |
| `health.import.cycle` | files depend on each other (a strongly connected set of ≥ 2 considered files) | `high` if every edge is `resolved`, else `medium` | the cycle's edges | medium |
| `health.dependency.hub` | fan-in ≥ `min_fan_in` (20) **and** in the top `top_percent` (5%) of considered files | `high` | a sample of incoming edges | low |
| `health.dependency.fan_out` | the same for fan-out (`min_fan_out` 20) | `high` | a sample of outgoing edges | low |
| `health.module.unreferenced` | no repository file depends on it. Never reported: `__init__.py`, `__main__.py`, `main.py`, `app.py`, `manage.py`, `wsgi.py`, `asgi.py`, `setup.py`, files with the `entrypoint` role, and tests | see below | the file | info |
| `health.dependency.unused_declared` | a Python project declares a `prod` or `optional` requirement (`REQUIRES`) that no file imports. Fires only when the extractor emits `REQUIRES`; the Python analyzer does not yet | see below | the `REQUIRES` edge | low |

**Unresolved imports decide confidence for absences.** "Nothing depends on X" or "nothing imports Y" can
only be claimed confidently when nothing was hidden (CIM §6).
- A **hiding file** is a considered-language source file (tests included) that has an unresolved import with
  reason `dynamic` or `not_found`, or whose extraction `failed` or was `partial`.
- If any hiding file exists, `health.module.unreferenced` and `health.dependency.unused_declared` are `low`,
  and their message says so.
- An unreferenced file that was not fully extracted itself is also `low`.

### Scope and metrics

* **Considered files.** Extracted (`full`/`partial`) files of the configured languages (Python) whose origin is
  `source`, excluding tests (role `test_file`, a `test`/`tests` directory, or `test_*.py`, `*_test.py`,
  `conftest.py`). Structural findings and file metrics are about these files.
* **Who counts as a dependent.** Every repository file, tests included: a module used only by tests is
  referenced.
* **Metrics** (rule `health.metrics`):
  - `fan_in` and `fan_out` per considered file: distinct repository files, not edges; external packages are
    not counted.
  - On the repository node `/`: `health_files_considered`, `health_blind_spot_files` and
    `health_hidden_import_files`.

### Parameters and identity

`HealthParams` holds the dependency kinds, languages, origins, test patterns, `min_fan_in`, `min_fan_out`,
`top_percent`, root module names, checked requirement scopes and the evidence sample size. `params_hash` is
sha256 of its canonical JSON, so changing a threshold makes a **new run**. A run is identified by
`(snapshot, "health", "1.0.0", params_hash)`.

### Storage, orchestration, API

* **Storage.** Health runs reuse the derivation tier unchanged: migration 0003 tables, `persist_derivation`,
  `load_derivation`, the immutability guards (docs/persistence/postgres-v1.md).
* **Orchestration.** `xraymyrepo.orchestration.derive_health(conn, snapshot_id, params=None)` loads, derives
  and stores, and returns the run id. An existing run raises `DerivationRunExistsError` without recomputing.
  There is no CLI.
* **API** (docs/api/api-v1.md). A health run is a derivation run:
  - `GET /api/v1/derivations/{run_id}/findings?rule=&subject=<key>` lists findings, optionally about one node
    or group;
  - `GET /api/v1/derivations/{run_id}/metrics?metric=fan_in&order=desc` ranks a metric;
  - `GET /api/v1/derivations/{run_id}` gives finding counts per rule. `lenses` and `layer_coverage` are empty
    or zero for health runs; they belong to the architecture algorithm.

### Golden expectations

`fixtures/golden/v1/expected_health.json` is the reviewed health of the golden document;
`tests/insights/test_health.py` compares it byte-for-byte (regenerate deliberately with `XRAY_UPDATE_GOLDEN=1`).

| Finding | Basis / confidence | Why |
| --- | --- | --- |
| `health.coverage.blind_spot` on `backend/app/legacy.py` | observed / high | extraction failed (`parse_error`) |
| `health.coverage.blind_spot` on `backend/app/plugins.py` | observed / high | one `dynamic` and one `not_found` unresolved reference |
| `health.module.unreferenced` on `backend/app/plugins.py` | inferred / **low** | nothing imports it, but 2 files hide imports |

* **No file cycle.** The `app ↔ app.users` cycle exists only between components (architecture).
* **No unused requirement.** `pytest` is a dev requirement.
* **No hubs** at the default thresholds.
* **The analyzer's output** for the golden repository yields the same findings.

### Limitations

* Python only, and import-level: the analyzer emits only `IMPORTS`.
* Thresholds are fixed defaults, not tuned per repository; change them through `HealthParams` (a new run).
* `unused_declared` is silent until an extractor emits `REQUIRES`.
* "Unreferenced" is static: plugins loaded by name, framework discovery or reflection make it a false
  positive. That is exactly when unresolved `dynamic` imports drop its confidence to `low`.
* No history-based hotspots: there is no git data.

## Change impact (`xraymyrepo.insights.impact`)

*If these nodes change, what is affected?* This is a static, evidence-backed estimate over one snapshot's graph.
It is **computed on request** and never stored. `impact(graph, seeds, params) -> ImpactResult` is pure and
deterministic. It runs over `xraymyrepo.graph.DependencyGraph`, which is built from a `SnapshotDocument` or
from the records persistence loads; both builds are equal.

### Traversal

* **Seeds** are node keys of any kind except the repository root, at most 50.
  - A seed stands for itself and everything declared in it: a file seed covers its symbols, a directory seed
    its files.
  - A **symbol seed stays precise.** Its file's other symbols are not "changed". The file is listed as changed
    at distance 0, and file-level imports of it are followed.
* **Breadth first, level by level:**

  | Edge | Followed | Distance |
  | --- | --- | --- |
  | `IMPORTS`, `INHERITS`, `CALLS` | backwards: whoever depends on an affected node is affected | +1 |
  | `TESTS` | backwards: the tests of an affected node are affected | +1 |
  | `HANDLES` | forwards: an endpoint is affected exactly when its handler is | +0 |

* **Dependents count at file level.** Imports are module-level facts, so once any node of a file is reached,
  the whole file and its symbols are affected.
* **Bounds.** `max_depth` (default 3, at most 10) and `max_nodes` (10 000).
  - `truncated`: the node bound stopped the traversal.
  - `beyond_depth`: dependents exist beyond the depth bound.
* **Witness path.** Every result carries one shortest path of real edges from a seed. Among equally short
  paths the most confident wins, then edge order (kind, source key, target key).
* **Confidence.** `high` when every edge on the path is `resolved` or `observed`; `medium` when one is
  `heuristic` (for example `HANDLES`, `TESTS`).
* **Results** are affected **files**: category `file`, or `test` (role `test_file` or a test path), plus
  **endpoints**. Each has its distance, confidence and path.

### Blind spots are reported, never added

A file with unresolved `dynamic` or `not_found` imports could depend on the seeds without any edge showing it,
and a file whose extraction `failed` or was `partial` has unknown dependencies. These files are listed as
**blind spots**, and their count appears in every impact summary. They are never added to the affected set:
nothing is invented. Blind spots don't depend on the seeds.

### Serving it

* **Loading.** `persistence.queries.load_graph_records` loads a snapshot's graph as plain records in four
  queries: nodes, edges, roles, unresolved imports.
* **Caching.** The API keeps a **bounded, thread-safe, per-process LRU cache** of `DependencyGraph`s by
  snapshot id: `XRAY_IMPACT_CACHE_SIZE`, default 4, and 0 disables it. A complete snapshot never changes, so
  a cached graph can't be stale. There is no external cache.
* **A warm request** runs one SQL statement (the snapshot check); a cold one adds the four loading queries.
* **Endpoints** (docs/api/api-v1.md):
  - `GET /snapshots/{id}/impact?node=…`: the report;
  - `GET /snapshots/{id}/impact/blind-spots`;
  - `GET /derivations/{run}/impact/groups?node=…&lens=…&parent=…`: affected files per group of an
    architecture lens, one level at a time.

### Golden expectations

Seed `backend/app/db.py`, depth 3:

| Distance | Affected | Confidence |
| --- | --- | --- |
| 0 | `db.py` | high |
| 1 | `users/models.py` (IMPORTS and INHERITS `db.py#Base`) | high |
| 2 | `users/service.py` | high |
| 3 | `users/routes.py`, `tests/test_users.py` (test) | high |
| 3 | endpoint `GET /users/{user_id}` (via `HANDLES` from `routes.py#get_user`) | medium |

* **Beyond depth.** `main.py` is 4 hops away, so `beyond_depth` is true at depth 3.
* **Blind spots:** `legacy.py` (failed) and `plugins.py` (dynamic, not_found).
* **The analyzer's output** gives the same files without the endpoint, because it emits no `HANDLES`.

### Limitations

* **Module level.** The analyzer emits only `IMPORTS`, so impact is per file, not per call path.
  `CALLS`, `HANDLES` and `TESTS` refine it when an extractor emits them.
* **"Affected" is static reachability**, not runtime behaviour. There is no tracing or coverage data.
* **Blind spots aren't narrowed to the seeds.** Any hiding file is reported, whatever the seeds.
* **The cache is per process**, so each API worker loads its own graphs.
