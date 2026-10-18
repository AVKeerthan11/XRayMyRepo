# Change risk v1

*How much could each changed file of a comparison break other code, judged only by what the two snapshots
show?* `xraymyrepo.insights.change_risk.change_risk(comparison, before, after, params)` assesses one complete
`SnapshotComparison` and returns a `ChangeRiskAssessment` (CIM §8, `src/xraymyrepo/cim/change_risk.py`). Like
the comparison it is pure and deterministic: CIM in, CIM out. It imports no database, API or orchestration
code (`tests/test_architecture.py`). Storage is `xraymyrepo.persistence` (docs/persistence/postgres-v1.md,
"Change risk"); see "Orchestration" below. The read-only API is `/api/v1/change-risks/…`
(docs/api/api-v1.md, "Change risk").

Identity: algorithm `change_risk`, version `1.0.0`, and the hash of `ChangeRiskParams`, on top of the
comparison's own identity.

## What it measures, and what it does not claim

It measures the observable potential of a change to break other code: what kind of change it is, whether an
interface changed, how far the dependency graph carries it, and a few recorded signals (roles, health
findings, imports left dangling).

It does **not** claim:
- a probability of defects;
- runtime behaviour or test execution: impact is static reachability;
- security risk;
- churn, ownership or history: no git data is stored.

No score or fact comes from an LLM. Every point names its rule and cites concrete records.

## Levels, points and confidence

| Level | Meaning |
|---|---|
| `low` | little observable potential to break other code |
| `medium` | a change other code depends on, or one to a sensitive file |
| `high` | a changed interface others depend on, or a change with broad reach |

* **Points.** Every factor adds a non-negative integer. An item's points are their sum.
* **Level from points.** With the default thresholds (`ChangeRiskParams`), 0–1 is `low`, from
  `medium_at` = 2 it is `medium`, and from `high_at` = 4 it is `high`. The run records both thresholds, so
  every level can be checked against its points.
* **Assessment level** is the highest item level, or `low` when nothing changed.
* **A level is a lower bound on what was observed.**
  * Factors only add points.
  * Caveats (below) lower *confidence* and never change points. The true risk can be higher than the level,
    never lower.
  * An *absence* ("no test depends on it") is scored only when it is reliable. Otherwise it is listed with
    0 points and the caveats that make it unreliable.
* **Confidence and caveats are separate from the level.** An item's confidence is the lowest its caveats
  allow (`high` without caveats).
* **`conclusive`** (derived, not stored) is true only for `high` confidence, no caveat and a level that is not
  capped. A capped test file is therefore never conclusive: its `low` comes from the cap, not from its points.
  An assessment is conclusive only when every item is.
* **`capped`** (derived, not stored) marks an item carrying `risk.cap.test_file`. Its points are kept
  uncapped.
* **Never "safe".** `low` with `conclusive: false` means *no risk was observed in what could be seen*. It is
  never evidence that a change is safe, and there is no `safe` or `unknown` level.

## Subjects

* **Files only.** Every file with a changed node (the file itself, or a declaration or endpoint in it) is a
  subject, keyed like the comparison's rows: the `to` key, else the `from` key. Directories, packages and
  external packages are not subjects.
* **Origins.** By default only files whose origins are all `source` are assessed (`subject_origins`). Other
  changed files (docs, generated, vendored) are counted in `excluded_by_origin`, not dropped silently.
* **Which snapshot.** A subject is assessed in `to`, or in `from` when it was removed. Every evidence item
  says which snapshot its keys belong to (`side`).

## Factors

| Rule | Fires when | Points | Evidence |
|---|---|---|---|
| `risk.change.kind` | always: the file's own change | removed 2 · modified 1 · moved 1 · added 0 | the file (both keys for a move) |
| `risk.change.interface` | a declaration of the file in `from` was removed, or its `signature_hash` differs | +2 | the declaration keys (`from`, and `to` for changed signatures) |
| `risk.impact.dependents` | non-test files depend on it within `max_depth` (`insights.impact`) | ≥ 1 → +1, ≥ `wide_dependents` (10) → +2 | each dependent with its witness path |
| `risk.impact.endpoints` | an endpoint is reached (registered in the file, or through `HANDLES`) | +1 | each endpoint with its path |
| `risk.change.sensitive_role` | the file or a declaration in it has role `manifest`, `config`, `schema`, `entrypoint` or `orm_entity` | +1 | the classified keys |
| `risk.health.hub` | the inline health derivation reports `health.dependency.hub` on the file | +1 | the finding's evidence |
| `risk.health.cycle` | the file is in a `health.import.cycle` finding | +1 | the cycle's edges |
| `risk.dependency.unresolved_after_removal` | see below | +2 | the `from` edge, and the dependent with the span of its unresolved import in `to` |
| `risk.test.no_dependent_test` | no test file depends on it, and nothing could hide one | +1 (0 with caveats when unreliable) | the file |
| `risk.cap.test_file` | the subject is itself a test file | 0; its level is capped at `low` | the file |

Details:

* **Impact.** Impact runs over `to`. For a removed file it runs over `from`, counting only dependents that
  still exist in `to`.
  * Its settings are `max_depth` (3) and `max_nodes` (10 000).
  * The dependency kinds and test patterns are those of the embedded `HealthParams`, so one set of
    parameters decides what a dependency and a test are.
* **Signatures.** Fingerprints are compared only when both snapshots have the same extractor version (as in
  the comparison). An added declaration is not an interface change.
* **Health is computed inline** with `ChangeRiskParams.health`, so every health threshold is in the params
  hash. It runs on `to`, and on `from` only when a removed file needs it.
* **The absence factor** applies to modified, moved and added files that are not tests. It is not listed for
  removed files.
* **Test-file cap.** It is scoped to the test subject: its factors and points stay as computed, and only its
  own level is capped.
  * A production file's level and the assessment's level are never lowered by it.
  * Tests are never counted as dependents.
  * A test that depends on a file only removes the absence point.
* **No mitigation (v1).** Dependents updated in the same comparison do not reduce a removed or changed
  file's risk. This is deliberately conservative: a clean, complete refactor of a widely used interface
  still scores `high`, and the reviewer judges it from the cited dependents and their own changes.

## Unresolved imports after a removal

A rise in `not_found` imports alone proves nothing. The factor fires only on an exact recorded match, for a
**removed file** `S` and a dependent `D`, when all of the following hold:

1. In `from`, `D` has an `IMPORTS` edge into `S` (the file or anything declared in it).
2. `D` survives: its node change is `same_key` (unchanged or modified, not moved, because moving changes what
   relative specifiers mean), and it was extracted (`full`/`partial`) in both snapshots.
3. In `to`, `D` has an unresolved import with reason `not_found`. It must come from a producer of the edge's
   evidence, and its `raw_text` must be exactly one of:
   * **any producer**: one of the edge's `specifiers`, as written. The module the import named no longer
     exists (`from .legacy import old` → `.legacy`, `import a.b` → `a.b`).
   * **Python producers only** (`python_import_producers`, default `python-extractor`, under its rule
     `py.import.namespace_package`): `specifier.name` for a name the edge imported. That is how the resolver
     records `from ns import name` when `ns` is a namespace package and `ns/name.py` is gone
     (docs/analyzer/python-v1.md §4).
4. The comparison is fully comparable. Otherwise resolution itself may differ: the factor is not scored and
   the item gets `resolution_not_comparable`.

**What it claims:** "an import that resolved into the removed file no longer resolves". It does not claim
breakage, because the import may be guarded (`try/except ImportError`, `TYPE_CHECKING`).

**What it cannot see**, and therefore never claims:
- **A removed declaration in a surviving file.** `from m import n` with `n` gone but `m` present resolves to
  `m` itself and records nothing (python-v1 §4, target rule 5).
- **`from package import submodule` in a regular package.** It falls back to `package/__init__.py` the same
  way.
- **Attribute and call usage**, without `CALLS` edges.

**Weaker signals are caveats, never points.** If a surviving dependent of a removed file gained `not_found`
imports that no exact match (of any removed file) explains, the item gets `dependent_resolution_changed`.

## Caveats

| Caveat | When | Confidence at most |
|---|---|---|
| `incomparable_snapshots` | the comparison lists `incomparable` reasons; on the assessment and every item | medium |
| `undetermined_changes` | an undetermined node change in the file, or an undetermined edge change touching it | low |
| `incomplete_extraction` | the file was not `full` in either snapshot (failed, partial, unsupported, excluded) | low |
| `hidden_dependencies` | the impact graph has blind spots: files that hide imports, or did not parse | medium |
| `impact_bounded` | the impact traversal was truncated or has dependents beyond `max_depth` | medium |
| `heuristic_paths` | the witness path of a counted dependent or endpoint uses a heuristic edge, or every dependent test's does | medium |
| `resolution_not_comparable` | a removed file has checkable dependents, but the snapshots are not fully comparable | medium |
| `dependent_resolution_changed` | a surviving dependent of a removed file gained unexplained `not_found` imports | medium |

* `hidden_dependencies` is repository-wide, like impact's blind spots. One file that hides imports lowers the
  confidence of every item assessed on that side.
* `heuristic_paths` on tests covers the case where a heuristic link to a test withholds the absence point.
  If that link were wrong, the risk would be higher.

## Parameters

`ChangeRiskParams`; all of them are in `params_hash`:

| Parameter | Default |
|---|---|
| `medium_at`, `high_at` | 2, 4 (`high_at` > `medium_at`) |
| `wide_dependents` | 10 |
| `max_depth`, `max_nodes` | 3, 10 000 |
| `subject_origins` | `source` |
| `sensitive_roles` | `manifest`, `config`, `schema`, `entrypoint`, `orm_entity` |
| `python_import_producers` | `python-extractor` |
| `health` | `HealthParams()` (also the dependency kinds and test patterns) |

The factor points themselves are fixed by the algorithm version.

## Checks

* **What `change_risk` checks:**
  * the comparison is complete and fits its two snapshots (the comparison's own `validate_against`);
  * after assessing, `validate_against(comparison, before, after, assessment)`: the run names the comparison;
    every subject is a file row of it with its change; every evidence key, edge ref and span exists in the
    snapshot its side names.
* **What the models check (CIM §8):**
  * points are the sum of the factors;
  * each item starts with `risk.change.kind`, and each rule appears at most once;
  * the level follows from the points and the run's thresholds (or the cap);
  * the confidence is what the caveats allow;
  * factor caveats are item caveats, and an unscored absence says why;
  * `incomparable_snapshots` appears exactly when the comparison is not fully comparable.

## Orchestration

`xraymyrepo.orchestration.assess_change_risk(conn, comparison_id, params=None)` loads a stored comparison
and both of its snapshots, runs `change_risk` and stores the result with `persist_change_risk`. It returns
the run id.

* **Checked first, before anything is loaded.** The comparison must exist (`ComparisonNotFoundError`) and be
  complete (`ComparisonNotCompleteError`).
* **Existing runs.** If the identity `(comparison, "change_risk", "1.0.0", params_hash)` is already stored in
  any status, the call raises `ChangeRiskExistsError` (with `run_id` and `status`) and computes nothing.
  Assessments are deterministic, so a stored one is never recomputed or replaced.
* **Not fully comparable** comparisons are not an error. They are assessed with the `incomparable_snapshots`
  caveat.
* **Inputs.** Removed files are assessed in the `from` snapshot, the rest in `to`; both snapshots are
  loaded. Health is computed inline by the algorithm: on `to`, and on `from` only when a removed file needs
  it. When nothing changed, it is not computed at all.
* **Transactions.** Storing is one transaction (a savepoint inside the caller's). A failure leaves nothing
  behind, and the identity stays free. The comparison and snapshots are immutable, so loading them needs no
  transaction. Two concurrent calls for one identity store it once; the other raises
  `ChangeRiskExistsError`.

There is no CLI.

## Fixture

`fixtures/comparison/v1/expected_change_risk.json` is the reviewed assessment of the comparison fixture (see
its `FIXTURE.md`). `tests/insights/test_change_risk.py` checks it whole, spells out its cases, and covers
every factor, threshold and caveat with synthetic snapshot pairs and the golden document (endpoints, roles,
heuristic edges).

## Limitations

* **Files only.** Declarations count toward their file. There is no per-declaration risk.
* **Absolute thresholds.** A 10 000-file monorepo and a 20-file service share the same "wide" bar.
* **Import level.** The analyzer emits only `IMPORTS`, so dependents are module-level, and usage of a removed
  or changed declaration is not seen (see "Unresolved imports after a removal").
* **Repository-wide blind spots.** `hidden_dependencies` isn't narrowed to the subject, as with impact.
* **One traversal per subject.** Cost grows with changed files × graph size, bounded by `max_nodes`.
* **Cost** (measured, no targets: `tests/test_large_change_risk_db.py`, two ~8 100-node snapshots, 256
  subjects including a 2 000-dependent hub and 50 dangling imports, local PostgreSQL 16, two runs).
  * `assess_change_risk` takes about 2.5 s: loading the comparison and both snapshots about 1.1 s, the
    assessment 0.83–0.89 s, storing 0.07–0.12 s, loading the result back 0.01–0.02 s.
  * Peak Python allocations: about 111 MiB end to end, of which about 56 MiB is each loaded snapshot and
    about 25 MiB the assessment.
  * Loading both whole snapshot documents dominates time and memory.
* **No mitigation** for dependents updated in the same change (see "Factors").
