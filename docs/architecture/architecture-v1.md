# Architecture intelligence v1: the first derivation

`xraymyrepo.architecture` derives an architectural view from one immutable CIM snapshot:
- logical **groups** and the files in them;
- the **dependencies between groups**;
- **group metrics**;
- inferred **layers** of files;
- **findings**: layer violations and dependency cycles.

The result is a CIM `Derivation` (contract: `src/xraymyrepo/cim/derivation.py`, CIM §8). It is computed after
extraction, is never mixed into snapshot facts, and is stored in PostgreSQL as an immutable derivation run.

```text
complete snapshot ──load_snapshot──▶ SnapshotDocument ──derive()──▶ Derivation ──persist_derivation──▶ PostgreSQL
                                                                                                           │
                                            /api/v1/derivations/{run_id}/…  ◀── persistence.derivation_queries
```

The questions it answers, and where the answer lives:

| Question | Answer |
| --- | --- |
| What are the major logical groups? | The `components` lens (inferred), plus the `directory` lens (observed structure) |
| What layers appear to exist? | `InferredLayer` per file, or no record (unclassified) |
| How strongly is it supported? | `confidence` plus one `derived` evidence item per signal, each naming its rule |
| What depends on what, between groups? | `GroupEdge` rollups per edge kind, at every level of a lens |
| What is observed or resolved versus inferred? | `GroupEdge.basis_counts` (the basis of the underlying facts) and the lens kind (whether the grouping itself is inferred) |
| What evidence supports a conclusion? | Evidence cites snapshot **node keys** and **edge refs**; nothing is restated as a fact |

## Principles

* **Separate from extraction.** `architecture` depends only on `cim`: no analyzer, no database
  (`tests/test_architecture.py` enforces it). Snapshot tables are never written.
* **Never fake facts.** Everything inferred has `basis = inferred`, a `confidence`, `provenance.rule` and
  `derived` evidence pointing at the facts used. The `directory` lens is the only non-inferred part: a file
  being in a directory is observed.
* **"Unclassified" is a valid outcome.** No layer is forced. A considered file without a layer is
  unclassified, and that is recorded explicitly (see Coverage).
* **Deterministic and reproducible.** `derive(doc, params)` is a pure function: no clock, no randomness,
  canonical ordering. The same snapshot, algorithm version and params always give the same derivation,
  whatever order the snapshot's collections are in (tested by shuffling).

## Lenses and groups

A lens is one tree of groups over the snapshot's files. A file is in at most one group per lens. Symbols
belong to their file's group.

### `directory` (kind `directory`, rule `arch.lens.directory`)

* **Groups.** One group per directory, plus the repository root, nested like the tree. Keys:
  `group:directory:<directory key>` and `group:directory:/`. Labels are directory names.
* **Members.** Every file goes to the group of its own directory. All files are members, whatever their
  language or origin.
* **Basis.** Groups and memberships are `observed` with `high` confidence. Evidence cites the directory node.

### `components` (kind `inferred`)

* **Projects** (rule `arch.components.python_project`). One group per Python package manifest (a `package`
  node with a PyPI-family ecosystem) that encloses component files. Key: `group:components:<package key>`.
  Label: the package name. Evidence cites the package node and its manifest.
* **Components** (rule `arch.components.python_package`). Derived from each file's `module_name`, using the
  package the module lives in:

  | Module | Component | Example |
  | --- | --- | --- |
  | in a subpackage | its top two package levels | `app.users.service` → `app.users` |
  | directly in a top-level package | that package | `app.db` → `app` |
  | a top-level module | itself | `manage` → `manage` |

  Key: `group:components:<project key or "/">#<component>`, for example
  `group:components:pkg:pypi:acme-backend#app.users`. The parent is the enclosing project, or none.
* **Scope.** Python files with a module name whose origin is `source` or `generated`, whatever their
  extraction status. Tests are included (as their own component); other files are unassigned in this lens.
* **Confidence:**

  | Confidence | When |
  | --- | --- |
  | `high` | A regular package (has `__init__.py`) with at least 2 files |
  | `medium` | A namespace package, or a single file |
  | `low` | A lone top-level module |

* **Evidence** cites the component's directory and its `__init__.py`.
* **Memberships** are `inferred`, with their group's confidence. They are justified by the group's rule and
  evidence (decision D3: `GroupMember` has no provenance of its own).

Group keys are stable within a snapshot, and across snapshots whenever directory or module structure is
unchanged. Labels are deterministic and never AI-generated.

## Group dependencies (`GroupEdge`)

* **Leaf edges.** These are snapshot edges whose two ends lie in two different files. A symbol counts as its
  file; an endpoint as its registration file. Edges to packages and external packages are not group edges,
  because groups only contain files.
* **Rollup.** Walking both files' group chains down from the lens roots, a leaf edge counts once at **every
  level where the two chains differ**, as `GroupEdge(source group, target group, kind)`. Where the chains
  agree, the edge is internal to that group.
* **Zoom levels.** The edges between the children of group X are exactly the group edges whose two ends are
  children of X. The top level of a lens plus "within" every group partition all group edges (tested).
* **What each rollup carries.** `count` (leaf edges), `basis_counts` (how many are `resolved`, `heuristic`, …;
  never averaged) and up to 20 `sample_edge_refs`.

## Metrics (`NodeMetric`, rule `arch.metrics.group`)

Per group, counting dependency kinds only (`IMPORTS`, `INHERITS`, `CALLS` by default):

| Metric | Meaning |
| --- | --- |
| `files` | Member files, including descendant groups |
| `internal_edges` | Dependency edges with both ends inside the group |
| `afferent` / `efferent` | Leaf dependency edges entering / leaving the group |
| `instability` | `efferent / (afferent + efferent)`; omitted when both are 0 |

## Layer inference (`InferredLayer`, rule `arch.layer.resolve`)

**Vocabulary (D1).** `interface` > `application` > `domain` > `infrastructure`:
- A layer may depend on itself and on the layers listed after it.
- `infrastructure → domain` is also allowed.
- ORM models are `infrastructure`.

**Scope (D8).** Extracted (`full`/`partial`) Python files of origin `source`, excluding tests. Tests are files
with the role `test_file`, under a `test`/`tests` directory, or named `test_*.py`, `*_test.py` or
`conftest.py`. Generated, vendored, docs, non-Python and unparsed files are out of scope.

**Signals.** Each signal is cited as one `derived` evidence item with its own rule:

| Rule | Kind | Signal | Cites |
| --- | --- | --- | --- |
| `arch.layer.framework_import` | strong | the file (or a symbol in it) depends on a listed external package: `fastapi`, `flask`, `click`, … → interface; `sqlalchemy`, `psycopg`, `redis`, `requests`, … → infrastructure | the edges and the external packages |
| `arch.layer.role` | strong | a snapshot role on the file or a symbol in it: `entrypoint`/`schema` → interface, `orm_entity` → infrastructure | the classified nodes |
| `arch.layer.name` | weak | the nearest of file stem and directory names in the name table (`routes`, `api`, `cli`, `schemas` → interface; `services` → application; `domain`, `entities` → domain; `db`, `repositories`, `adapters` → infrastructure). `models` is deliberately absent | the file or directory |
| `arch.layer.direction` | graph | dependency edges to or from files whose *strong* layer allows the candidate layer | the supporting edges |

**Resolution:**

| Situation | Result |
| --- | --- |
| Two different strong layers | unclassified (no record) |
| One strong layer W, and the name disagrees or any edge contradicts W | W, `low` |
| One strong layer W, and ≥ 2 signal kinds (framework, role, name) agree | W, `high` |
| One strong layer W, otherwise | W, `medium` |
| No strong layer, one name layer N with ≥ 1 supporting edge and none contradicting | N, `medium` |
| A name alone, or nothing | unclassified (D2: a name is a convention, not an inference) |

Layers are file-level. Symbols take their file's layer. Directory and group layer mixes are computed when
read (the contract does not allow layers on groups).

**Coverage.** Every considered file gets the metric `layer_considered = 1`. The repository node `/` gets
`layer_files_considered`, `layer_files_classified` and `layer_files_unclassified`. So a file is:
- **classified** if it has an inferred layer;
- **unclassified** if it was considered and has no layer;
- **out of scope** otherwise.

## Findings (D5)

| Rule | When | Subjects | Confidence | Evidence |
| --- | --- | --- | --- | --- |
| `arch.layer.violation` | a dependency edge from a file of one inferred layer to a layer it may not depend on (one finding per file pair) | the two files | the lower of the two layers' | the violating edges |
| `arch.group.cycle` | groups of the `components` lens that depend on each other (a strongly connected set of ≥ 2) | the group keys | the lowest group confidence | the sample edges of each group edge in the cycle |

Both have basis `inferred` and severity `medium`.

## Derivation identity, params, persistence

* **Identity.** A run is identified by `(snapshot, algorithm = "architecture", algorithm_version = "1.0.0",
  params_hash)`.
* **Params.** `ArchitectureParams` holds every rule table, scope setting and the layer order. `params_hash` is
  `sha256` of its canonical JSON, the same formula as `AnalysisConfig.config_hash`. Changing any table
  changes the hash; changing the algorithm bumps its version. Either way the result is a **new** run; runs
  are never updated.
* **Persistence.** Tables come from migration 0003 (see docs/persistence/postgres-v1.md, "Derivations"):
  - pending → complete in one transaction;
  - immutable once complete;
  - only complete snapshots can be derived;
  - deleting a snapshot removes its runs;
  - `load_derivation(persist_derivation(d)) == d.canonical()`.
* **Orchestration.** `xraymyrepo.orchestration.derive_architecture(conn, snapshot_id, params=None)` loads,
  derives and persists, and returns the run id. It checks the run identity first: an existing run raises
  `DerivationRunExistsError` without recomputing (derivations are deterministic). It is the only module that
  imports both `architecture` and `persistence`.

## Running derivations

```python
import psycopg
from xraymyrepo.architecture import ArchitectureParams, derive
from xraymyrepo.orchestration import derive_architecture
from xraymyrepo.persistence import load_derivation

# In memory, no database:
derivation = derive(document)

# Against a database migrated with db/migrations:
with psycopg.connect(url, autocommit=True) as conn:
    # Optionally pass ArchitectureParams(...) to use other rule tables (a different run).
    run_id = derive_architecture(conn, snapshot_id)
    stored = load_derivation(conn, run_id)  # == derive(load_snapshot(conn, snapshot_id))
```

Then read it through the API: `GET /api/v1/snapshots/{snapshot_id}/derivations` and
`/api/v1/derivations/{run_id}/…` (docs/api/api-v1.md). There is no CLI in v1 (D7).

Tests: `pytest tests/architecture` needs no database. The persistence, query and API tests of derivations
(`tests/test_derivations_db.py`, `tests/test_derivation_queries_db.py`,
`tests/api/test_derivations_api_db.py`) need a disposable PostgreSQL 16. See docs/persistence/postgres-v1.md,
"Running the tests".

## Golden expectations

`fixtures/golden/v1/expected_architecture.json` is the reviewed, canonical derivation of the golden document.
`tests/architecture/test_derive.py` compares it byte-for-byte; regenerate it deliberately with
`XRAY_UPDATE_GOLDEN=1` and review the diff.

**Components.** `acme-backend` contains:
- `app` (high, 5 files)
- `app.users` (high, 5 files)
- `app.generated` (medium)
- `tests` (medium)

**Layers:**

| File | Layer | Confidence | Signals |
| --- | --- | --- | --- |
| `users/routes.py` | interface | high | name + fastapi |
| `main.py` | interface | high | fastapi + `entrypoint` |
| `users/schemas.py` | interface | high | name + `schema` |
| `db.py` | infrastructure | high | name + sqlalchemy |
| `users/models.py` | infrastructure | high | sqlalchemy + `orm_entity` |
| `users/service.py` | application | medium | name + direction |

* **Unclassified (3).** `plugins.py` and the two `__init__.py` files. `legacy.py` (unparsed), generated code,
  tests and non-Python files are out of scope.
* **Findings.** One `arch.group.cycle` between `app` and `app.users`: `main.py → users/routes.py` one way,
  `users/models.py → db.py#Base` (IMPORTS and INHERITS) the other. There are no layer violations.
* **The analyzer's own output** for the same repository has fewer facts (no roles but `manifest`, IMPORTS only),
  so it gets lower confidences: `main.py`, `models.py` and `schemas.py` are medium. It invents no layers and
  finds the same cycle.

## Decisions (Milestone 5 review)

| # | Decision |
| --- | --- |
| D1 | Layers `interface/application/domain/infrastructure`; ORM models are infrastructure; tests are out of scope |
| D2 | A name-only signal never produces a layer |
| D3 | Memberships are justified by their group's provenance and evidence; no contract change |
| D4 | Lenses `directory` and `components`; the `package` lens is deferred |
| D5 | Findings: only `arch.layer.violation` and `arch.group.cycle` |
| D6 | Metrics: `files`, `internal_edges`, `afferent`, `efferent`, `instability`, plus layer coverage |
| D7 | A Python orchestration function, no CLI |
| D8 | Layering scope: Python source files; generated, vendored and test files excluded |
| D9 | Separate read-only `/api/v1/derivations/{run_id}/…` routes |
| D10 | Migration 0003 adds tables only; existing tables and the CIM schema version are unchanged |

The CIM contract is unchanged. The only CIM code addition is `Derivation.canonical()` and
`to_canonical_json()`, which are ordering helpers like `SnapshotDocument.canonical()`.

## Limitations (v1)

* **Python only.** Components and layers cover Python. Other languages appear only in the directory lens.
* **Rule tables are conventions.** Framework and name tables are short, fixed lists, so unknown frameworks
  give no signal. A file importing both an interface and an infrastructure framework is unclassified.
* **The direction signal only uses files with a strong layer.** Layers do not propagate through
  name-only files.
* **Components fold deeper subpackages.** Components are the top two package levels. A core package such as
  `app` contains both its composition root (`main.py`) and shared infrastructure (`db.py`), which is why the
  golden repository shows an `app ↔ app.users` cycle. Splitting composition roots is deferred.
* **Group edges cover only edges between files.** Dependencies on external packages are not rolled up
  (members are files), and edges inside one group are counted only as the `internal_edges` metric.
* **Few relationship kinds.** The analyzer emits only IMPORTS today, so CALLS, INHERITS, HANDLES and TESTS
  signals appear only with richer extractors (as in the golden document).
* **Layer counts are per file.** Layer mixes count files, not lines or symbols.

## Deferred (not in v1)

- the `package` lens;
- graph clustering, or any non-deterministic or similarity-based grouping;
- directory- or group-level layer records;
- more finding rules (fan-in/fan-out hotspots, unstable dependencies, …);
- AI labels or explanations (the interpretation tier);
- runtime tracing;
- service or deployment inference;
- `READS_FROM`/`WRITES_TO`/`USES_TYPE`;
- fuzzy lineage, blame, PR intelligence and git history;
- a CLI for derivations;
- the frontend visualization.
