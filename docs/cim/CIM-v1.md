# Codebase Intelligence Model (CIM) v1

Status: **contract v1.0**. Code: `src/xraymyrepo/cim/`. Schema: `db/migrations/0001_cim_v1_snapshot_tier.sql`.
Golden fixture: `fixtures/golden/v1/`. Any change to a vocabulary or rule below is a contract change and must
update the code, the SQL CHECK constraints (kept equal by `tests/test_schema_consistency.py`) and this document.

## 1. What the CIM is

The CIM is a model of one repository at one commit: what exists, how it relates, and *how we know*.
It exists so that visualization, impact analysis, tracing and AI all reason over the same facts, and so that
no consumer ever presents a guess as a fact.

It has three tiers. Each is immutable and versioned independently:

| Tier | Contents | Reproducible from | Allowed basis |
|---|---|---|---|
| **Snapshot** (`SnapshotDocument`) | nodes, edges, classifications, unresolved references, extraction issues | commit + extractor version + config | observed, resolved, heuristic |
| **Derivation** (`Derivation`) | lenses, groups, rollups, metrics, findings, inferred layers | snapshot + algorithm version + params | observed … inferred |
| **Interpretation** (`Annotation`) | AI labels, summaries, explanations | not reproducible; append-only | interpreted (implicit) |

The snapshot document is the interchange format between extractors, the database loader and the golden
fixture. It refers to nodes by **key** and to edges by **(kind, source key, target key)**. Database ids never
appear in the contract.

## 2. Node taxonomy

Nine closed kinds. Nothing else is a node.

| Kind | What | Parent (`parent_key`) | Key example |
|---|---|---|---|
| `repository` | the root | none | `/` |
| `directory` | a directory | repository or directory | `src/app` |
| `file` | a file | repository or directory | `src/app/users/service.py` |
| `type` | `type_kind`: class, interface, struct, enum, trait, protocol, type_alias | file, type, function | `src/app/users/service.py#UserService` |
| `function` | `function_kind`: function, method, constructor, accessor | file, type, function | `src/app/users/service.py#UserService.create` |
| `endpoint` | `protocol`: http, grpc, graphql, websocket, cli, message | the file where it is **registered** | `endpoint:http:GET /users/{id}@pkg:npm:@acme/api` |
| `package` | a unit declared by a manifest (`pyproject.toml`, `package.json`) | none | `pkg:npm:@acme/web` |
| `external_package` | a dependency outside the repository | none | `ext:npm:react` |
| `group` | a lens projection (**derivation tier only**, never in a snapshot) | another group | `group:directory:src/app` |

**Containment is `parent_key`, not an edge.** It is the *syntactic declaration tree*: where something is
written, not who semantically owns it. There is exactly one tree. Packages, groups and external packages are
outside it.

Things that are deliberately **not** node kinds:

| Concept | Represented as |
|---|---|
| module | `file.attributes.module_name` (`app.users.service`) |
| method | `function` with `function_kind = method` |
| route | `endpoint` (protocol-generic, framework in attributes) |
| data model / ORM entity | classification `role = orm_entity` (or `schema` for Pydantic/Zod) on a `type` |
| config file, manifest | classification `role = config` / `manifest` on a `file` |
| test suite / test case | classification `role = test_file` / `test_function` |
| generated / vendored code | classification `origin = generated` / `vendored` |
| component, subsystem, service | `group` in a lens |
| layer | heuristic `layer` classification (snapshot) or `InferredLayer` (derivation, §8) |
| variables, fields, decorators | not modelled; an import of a variable targets its file |

### Node fields

`key, kind, name, parent_key, language, span, basis, confidence, provenance, evidence[],
extraction_status (files), blob_sha (files), content_hash, signature_hash (types/functions), attributes`.

* `span` is required on type, function and endpoint nodes and forbidden elsewhere. Lines are 1-based; columns
  are 0-based Unicode code points; `end_col` is exclusive. Symbol spans include decorators.
* `name` is determined by the key: last path segment, last qualified-name segment, endpoint name, package name.
* Symbols and endpoints have the language of their file and exist only in extracted files.

## 3. Edge taxonomy

Six closed, stored kinds. Direction is always `source → target` and reads as a sentence with the source as
the active party. Inverse edges are never stored. Impact analysis traverses them in reverse.

| Kind | Source → target | Basis | Attributes |
|---|---|---|---|
| `IMPORTS` | file/type/function (innermost scope) → file/type/function/external_package (most specific resolved node) | resolved, heuristic | `specifiers`, `imported_names`, `is_type_only`, `is_reexport`, `is_dynamic`, `via` |
| `REQUIRES` | package → external_package/package | observed | `scope` (prod, dev, optional, peer), `version_constraint` |
| `INHERITS` | type → type/external_package | resolved, heuristic | `mode` (extends, implements, mixin), `target_symbol` (required for external targets) |
| `HANDLES` | function/type → endpoint | heuristic | `framework` |
| `TESTS` | file/type/function → file/type/function | heuristic (v1 default) | none |
| `CALLS` | file/function → function/type/external_package | resolved, heuristic | `call_kind`, `target_symbol`. Part of the schema, but **not extracted in v1** |

Rules:

* An edge is unique per snapshot on `(kind, source, target)`. Repeated occurrences are one edge with
  `occurrence_count` and a capped evidence sample (≤ 20 items, primary first).
* Only `CALLS` may be a self-loop (recursion).
* Barrel / re-export files are resolved **through**. `index.ts` importing `./orders` targets
  `orders/handlers.ts#getOrder` with `via: ["frontend/src/orders/index.ts"]`. There is no edge to the barrel.
* **A stored edge needs a concrete target.** Seeing `from .database import User` is an observed *source
  fact*, but no `IMPORTS` edge exists until the reference resolves to a node; the edge is then `resolved`
  (or `heuristic` when a rule chose the target). A syntactic reference that does not resolve is an
  `UnresolvedReference`, never an `observed` edge or an invented node. That is why `IMPORTS`, `INHERITS` and
  `CALLS` have no `observed` basis. `REQUIRES` is `observed` because the manifest names its target directly.
* `TESTS` is `heuristic` in v1 (naming and import conventions). Future runtime or coverage evidence may
  justify `observed` TESTS edges; that would be a contract change.
* The allowed basis per kind is enforced in Python (`EDGE_RULES`) and in SQL (`edge_kind_basis_check`).
* One reference resolving to several candidates is stored as several edges that share an `ambiguity_group`,
  each with `confidence = low`.

Not stored: `DECLARES`/`CONTAINS` (= `parent_key`), `MEMBER_OF` (= lens membership), `DEPENDS_ON`
(= derivation rollups), `OVERRIDES` (derivable). Deferred: `REFERENCES`, `USES_TYPE`, `READS_FROM`, `WRITES_TO`.

## 4. Identity

A **key** is the node's *declared identity*: where it is declared plus what it is called. It never contains
line numbers or the snapshot id. Only one rare disambiguator mentions a declaration space. Keys are unique per
snapshot.

```
repository        /
directory, file   <path>                         src/app/users/service.py
type, function    <path>#<seg>(.<seg>)*          src/app/users/service.py#UserService.create
segment           <name>[~type][@<n>]            Foo~type   f@2   Outer@2.method
endpoint          endpoint:<protocol>:<name>@<scope>
                                                 endpoint:http:GET /users/{id}@pkg:npm:@acme/api
scope             <package key> | /              pkg:pypi:acme-backend   /
package           pkg:<ecosystem>:<name>         pkg:npm:@acme/web
external_package  ext:<ecosystem>:<name>         ext:pypi:sqlalchemy
group             group:<lens>:<local>           group:directory:src/app
```

* Paths are POSIX, repository-relative, NFC-normalized and case-preserving. `%`, `#` and `:` in paths are
  escaped as `%25`, `%23` and `%3A`, so path keys never contain `:` and cannot collide with prefixed keys.
* `~type` marks a type-space declaration sharing a name with a value in the same scope (TS `interface Foo` +
  `const Foo`). `@n` (n ≥ 2) numbers repeated declarations of one name in one scope (`if PY2: def f …`).
* Overloads are one node. Anonymous functions are not nodes; their contents belong to the enclosing named scope.
* HTTP routes are canonical: an upper-case method and `{param}` syntax (`/orders/:id` → `/orders/{id}`). The
  as-written route is kept in `raw_route`.
* **Endpoint keys are scoped** so that `GET /health` in two packages is two endpoints. The scope is the key of
  the nearest package whose root encloses the endpoint's registration file (`attributes.scope_key`), or `/`
  (the repository) when no package does. The node `name` is the unscoped endpoint name (`GET /health`).
  When several packages share that nearest root, the ones whose ecosystem serves the file's language win
  (`pypi`: python; `npm`: javascript, typescript; `ECOSYSTEM_LANGUAGES`). If that leaves several, or none
  match, the candidate with the smallest key in code point order is the scope. `endpoint_scope()` implements
  this rule for both extractors and the document checker.
* **Duplicate registrations.** The same endpoint registered several times in one scope is one node. Its
  `parent_key` and `span` are the earliest registration by (file key, line, column), keys compared in code
  point order (as `COLLATE "C"`). Every registration, the primary one included, is a span evidence item
  produced by the endpoint's own rule (`provenance.rule`); other evidence, such as a router prefix, uses other
  rules. A registration list longer than the evidence cap keeps the earliest 20.
  Parsing splits at a trailing `@/`, otherwise at the last `@pkg:`, so routes may contain `@`. Deployment
  boundaries are not modelled in v1; when they are, they may become a scope.
* **Endpoint identity is semantic**: protocol, canonical name and scope. It never contains the handler, so
  pointing a route at a different function keeps the key; handlers are `HANDLES` edges.
* PyPI names are PEP 503-normalized (`SQLAlchemy` → `sqlalchemy`). Versions are attributes, never identity.
* Symbol keys mirror the tree: the parent of `f.py#A.b` is `f.py#A`, and the parent of `f.py#A` is `f.py`.

**Keys change when code moves or is renamed. That is by design.** Following an entity across snapshots is
done by **lineage** (§8), using fingerprints:

* `content_hash` = sha256 over the language extractor's normalized token stream (no whitespace or comments).
* `signature_hash` = sha256 over the symbol's shape (parameters, return type, kind; for types the bases),
  which excludes the symbol's own name.

## 5. Evidence and provenance

Every edge and classification has ≥ 1 evidence item. A node has evidence when its basis is not `observed`
(endpoints, packages, externals). Each evidence item is one independent support for the claim. It carries:

* `basis`: the basis of the claim *as asserted through this item*;
* `provenance`: `{producer, rule}`. `producer` names a `Producer(name, version, config_hash)` declared by the
  snapshot. `rule` is the rule id (`py.import.relative`, `py.fastapi.route_decorator`) that answers "why does
  this exist?". The rule is mandatory for heuristic and inferred claims and for convention evidence.

| type | Fields | Example |
|---|---|---|
| `span` | `file_key, start_line, start_col, end_line, end_col, ast_type` | the `from ..db import Base` statement |
| `config` | `file_key, json_pointer` (RFC 6901 into the *parsed* data, also for TOML) | `pyproject.toml` `/project/dependencies/1` |
| `convention` | `pattern, matched_value` (rule = `provenance.rule`) | `test_*.py` matched `test_users.py` |
| `derived` | `node_keys[]`, `edge_refs[]` | `role = orm_entity` derived from two INHERITS edges |
| `ai` | `model, prompt_version, cited_evidence_refs[]` | interpretation tier only |

Evidence points at source and never embeds it. Text is fetched from the file's `blob_sha` using the span.

Several producers can support one record: their items are appended. A record's `basis` is the **strongest**
basis among its items (observed > resolved > heuristic > inferred > interpreted). Conflicting classifications
(two origins for one file) are kept as separate rows, all marked `contested`. Ambiguous resolution uses
`ambiguity_group` (§3).

```json
{"kind": "IMPORTS", "source_key": "backend/app/users/models.py", "target_key": "backend/app/db.py#Base",
 "basis": "resolved", "confidence": "high",
 "evidence": [{"type": "span", "basis": "resolved",
               "provenance": {"producer": "python-extractor", "rule": "py.import.relative"},
               "file_key": "backend/app/users/models.py",
               "start_line": 4, "start_col": 0, "end_line": 4, "end_col": 21,
               "ast_type": "import_from_statement"}],
 "attributes": {"specifiers": ["..db"], "imported_names": ["Base"]}}
```

## 6. Basis and confidence

**Basis** is *how* a claim is known:

| basis | meaning | example |
|---|---|---|
| `observed` | read directly from syntax or configuration | a class declaration; a manifest dependency |
| `resolved` | an observed reference linked to a concrete target | `from ..db import Base` → `db.py#Base` |
| `heuristic` | a framework, naming or convention rule matched | `@router.get` ⇒ HTTP endpoint; `test_*.py` ⇒ test file |
| `inferred` | computed algorithmically over the graph | clusters, `InferredLayer` (derivation tier only) |
| `interpreted` | produced by AI | names, summaries (interpretation tier only) |

**Confidence** is `high | medium | low`: how well supported the claim is *within* its basis. It is categorical
on purpose. No 0–1 score is stored, because nothing is calibrated. Observed claims are always `high`, and
ambiguous candidates are always `low`.

Visual trust levels follow basis: solid = observed/resolved, qualified = heuristic, speculative =
inferred/interpreted.

### What could not be seen

Absence of an edge means "no such relationship" only if all of these hold:

1. the file's `extraction_status` is `full` (the other values are `partial`, `failed`, `unsupported` and `excluded`);
2. the snapshot's `coverage` declares that edge kind for the file's language;
3. no `UnresolvedReference` of that kind exists in scope.

`UnresolvedReference(source_key, file_key, ref_kind, raw_text, span, reason, candidates, provenance)` has
`reason ∈ not_found | dynamic | ambiguous | unsupported_language`. `ExtractionIssue` records parse errors and
other problems; a `failed` or `partial` file always has an error issue.

## 7. Snapshot model

A snapshot's identity is `(repository, commit_sha, extractor_version, config_hash)`, where
`config_hash = sha256(canonical JSON of AnalysisConfig)`. `AnalysisConfig` holds the enabled languages, the
**explicit** exclusion policy per origin, and ignore globs. Default policy: `source` and `generated` →
`extract`; `vendored`, `docs` and `build_artifact` → `file_only`.

* The lifecycle is `pending → complete | failed`, one way. Models are frozen. The database trigger
  `snapshot_guard_immutable` rejects any change to identity, config or coverage, and any change to a
  non-pending snapshot.
* Re-analysis (new commit, extractor version or config) always produces a **new** snapshot.
* Every file has an `origin` classification. Files whose origin is `file_only` by policy have
  `extraction_status = excluded` and no symbols. Generated code is extracted and marked.
* Repository-level history (commits, file changes, contributors) is not part of the snapshot and not part of
  this migration.

## 8. Derivation layer (contract only in v1)

`DerivationRun(snapshot, algorithm, algorithm_version, params_hash, status)` produces:

* `Lens(name, kind ∈ directory | package | inferred)`: one tree of groups over the snapshot's files.
* `Group(key, parent_key, label, basis, confidence, provenance, evidence)`. In an **inferred** lens every group
  and membership has basis `inferred`; in other lenses none do. Labels are deterministic; AI names are
  annotations.
* `GroupMember(group_key, node_key)` assigns **files**; symbols belong to their file's group. A file is in at
  most one group per lens.
* `GroupEdge(source_group, target_group, edge_kind, count, basis_counts, sample_edge_refs)`: rollups between
  groups of one lens. Confidence is never averaged: `basis_counts` (e.g. `{resolved: 40, heuristic: 2}`) sums
  to `count`.
* `NodeMetric(node_key, metric, value, provenance)` and `Finding(rule, severity, subject_keys, message,
  basis, confidence, evidence)`.
* `InferredLayer(node_key, layer, basis, confidence, provenance, evidence)`: a layer an algorithm computed from
  the graph. Basis is always `inferred`, the rule is required, evidence is `derived` evidence with basis
  `inferred`, and a node has at most one per run.

**Layers in two tiers.** A snapshot `layer` classification is *heuristic* evidence only: a naming, layout or
config rule matched (`**/services/**` ⇒ `domain`). It must have basis `heuristic` and may not cite `derived`
evidence, because a layer computed over the graph is an inference, not a snapshot fact. Such layers are
`InferredLayer` records of a derivation run, explicitly marked `inferred`, and can never pass for snapshot facts.

`NodeLineage(from_snapshot, from_key, to_snapshot, to_key, match_method, confidence)` links nodes across two
snapshots of one repository. The v1 methods are `same_key` (high), `git_rename` (high) and `same_content_hash`
(high or medium). Unmatched nodes are added or removed; nothing is invented. There is no fuzzy matching in v1.

Known v1 limitation: endpoint keys contain their package scope, and endpoints have no fingerprints, so
renaming a package (or moving an endpoint to another package) shows its endpoints as removed and added. There
is no endpoint-specific or fuzzy matching to bridge that.

## 9. Interpretation layer

`Annotation(id, snapshot, subject, kind ∈ label | summary | explanation, content, model, prompt_version,
citations[≥1], created_at, superseded_by)`. The subject is a node (including a group) or an edge. Citations
are `NodeRef`, `EdgeRef` or `SpanRef`.

**AI never creates graph facts.** This is enforced structurally:

* snapshot nodes, edges and classifications reject basis `interpreted` and `ai` evidence (in Python and in the
  SQL CHECK constraints);
* derivation records reject basis `interpreted`;
* `SnapshotDocument` and `Annotation` forbid extra fields, so annotations cannot carry nodes or edges and
  documents cannot carry annotations;
* revisions are append-only (`superseded_by`); annotations are frozen.

## 10. Not supported in v1 (intentionally)

* Extraction of any kind: no parsers, no Tree-sitter, no TypeScript compiler, no import resolution, no call
  graph. The contract and golden fixture define what extractors must produce.
* `CALLS` extraction (schema only), `USES_TYPE`, `REFERENCES`, `READS_FROM`, `WRITES_TO`.
* Variables, fields and decorators as nodes; external symbols as nodes; anonymous-function nodes.
* Resource nodes (databases, queues, external services); service or deployment inference.
* Clustering, layering inference and computed lenses; derivation and interpretation database tables.
* Fuzzy lineage, git history ingestion, blame, contributors. Endpoint lineage across package renames.
* Partial classes or declarations spanning files (would break the single declaration tree).
* Neo4j or any graph database. PostgreSQL is the system of record; graph traversal will run in memory over
  immutable snapshots.

## 11. Locked decisions (review of 2026-10-07)

1. **Endpoint keys are scoped**: `endpoint:<protocol>:<name>@<scope>`, scope = nearest enclosing package key,
   else `/`. Identity is semantic and never names the handler (§4).
2. **Inferred layers live in the derivation tier** (`InferredLayer`, basis `inferred`). Snapshot `layer`
   classifications are heuristic and never rest on `derived` evidence (§8).
3. **Edge bases**: IMPORTS, INHERITS, CALLS: resolved | heuristic. REQUIRES: observed. HANDLES, TESTS:
   heuristic. Unresolvable references are `UnresolvedReference`s, never edges (§3).
4. **Shared package roots**: the package ecosystem must match the file's language; otherwise, the smallest
   candidate key (§4).
5. **Duplicate endpoint registrations**: earliest (file key, line) is the parent; the rest are span
   evidence (§4).
6. **Endpoint lineage across package renames** is a v1 limitation; no fuzzy endpoint matching (§8).
7. **Repository scope token** is `/` (`endpoint:http:GET /health@/`).
8. **Golden fixture** carries no `layer` classifications until layer convention rules are agreed.

## Appendix: database (snapshot tier)

Tables: `repository`, `producer`, `snapshot`, `snapshot_producer`, `node`, `edge`, `classification`,
`unresolved_reference`, `extraction_issue`, `schema_migration`.

* Closed vocabularies are `text` + `CHECK`, never `CREATE TYPE … AS ENUM`.
* Snapshot-scoped tables have `PRIMARY KEY (snapshot_id, id)`, and every FK between them leads with
  `snapshot_id`, so they can later be partitioned by snapshot without reshaping.
* `node.key` is `COLLATE "C"`. The unique `(snapshot_id, key)` index also serves subtree prefix queries
  (`key LIKE 'src/app/%'`).
* `edge` is unique on `(snapshot_id, source_id, kind, target_id)` (forward traversal), with an index on
  `(snapshot_id, target_id, kind)` (reverse traversal). `edge_kind_basis_check` mirrors the per-kind basis
  table in §3.
* Filterable fields are real columns (kind, basis, confidence, span, extraction status, blob sha, fingerprints).
  Evidence and kind-specific attributes are JSONB, and are not GIN-indexed.
* Evidence JSONB stores node **keys** and producer **names**, exactly as in the contract. Within a snapshot both
  resolve to one row (`node_key_unique`, `snapshot_producer_name_key`).
