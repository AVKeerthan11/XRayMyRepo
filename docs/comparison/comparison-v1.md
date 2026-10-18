# Snapshot comparison v1

What changed between two immutable snapshots of one repository. The records are the CIM comparison models
(docs/cim/CIM-v1.md §8, `src/xraymyrepo/cim/comparison.py`). The algorithm is
`xraymyrepo.insights.comparison.compare(before, after, params)`: pure and deterministic, CIM in, CIM out. It
imports no database, API or orchestration code (`tests/test_architecture.py`). Storage is
`xraymyrepo.persistence` (docs/persistence/postgres-v1.md, "Comparisons"); see "Orchestration" below. The
read-only API is `/api/v1/comparisons/…` (docs/api/api-v1.md, "Snapshot comparison").

Identity: algorithm `comparison`, version `1.0.0`, and the hash of `ComparisonParams`:

| Parameter | Default | Meaning |
|---|---|---|
| `move_languages` | `("python",)` | languages whose files can be move-matched |
| `min_move_symbols` | `1` | declarations (type/function nodes) a file needs to be move-matched |

## Principles

* **Only what the snapshots show.** Keys and fingerprints are compared. Nothing is fuzzy-matched, and no
  runtime or symbol-level relationship the analyzer does not extract is invented.
* **Never guess.** When the snapshots cannot tell, the answer is `undetermined` (confidence `low`). When a move
  is ambiguous, it is not made.
* **Honest confidence.** Decided changes are `high`. Moves are `medium`, and so are edges with a moved end.
  When the snapshots are not fully comparable (different extractor version or configuration), every change
  is at most `medium`. `NodeLineage` keeps its contract: `same_key` lineage is always `high`.

## Nodes

### 1. Same key

A key present in both snapshots is one node (`same_key`). What makes it `unchanged` or `modified`:

| Kind | Unchanged when |
|---|---|
| file | `blob_sha` is equal |
| type, function | `content_hash`, `signature_hash` and attributes are equal |
| repository, directory, endpoint, package, external package | attributes are equal |

* Spans, evidence and provenance never count, so moving a function within its file is not a change.
* A comment- or whitespace-only edit changes the file's blob (`modified`), but not the fingerprints of its
  declarations (`unchanged`). See docs/analyzer/python-v1.md §3.1.
* Fingerprints are compared only when both snapshots have the **same extractor version** and both nodes carry
  them. Otherwise a declaration is `unchanged` (at most `medium` across versions) when its file's blob is
  equal, and `undetermined` when it is not. This covers 0.1.0 snapshots (no fingerprints) against 0.2.0.
* A key whose node kind changed (a directory replaced by a file named the same) is `modified`, recorded
  with its kind in `to`.

### 2. Moved files (Python first)

A removed file and an added file are one **moved** file (`same_content_hash`, `medium`) only when:

1. the blob is **eligible** in both snapshots: not the empty blob, a `move_languages` file, declaring at least
   `min_move_symbols` type/function nodes;
2. the blob occurs **exactly once in each whole snapshot**. This rules out copies: content that also stays
   where it was is not a move.

**Not move-matched:** non-Python files (Markdown, TOML, …), Python files without declarations (constants
only, empty `__init__.py`), and a file that was moved *and* edited. They are `removed` + `added`. No content
similarity is computed.

The **declarations of a moved file** move with it when the same key below the new path exists only in `to`
with the same node kind (`old.py#A.f` → `new.py#A.f`). Endpoints are not keyed by file, so they match by key.

**Ambiguous** (`move_ambiguous` on an added/removed file): the file is eligible and its content is on the other
side, but not uniquely (two identical candidates, or a copy left behind). It is reported, never matched.

### 3. Unmatched nodes

Unmatched nodes are `removed` or `added`, except one whose **containing file still exists on the other side but
was not extracted there** (failed, unsupported or excluded). That one is `undetermined` with its one key. The
declarations of a file that stopped parsing are not "removed": the snapshot cannot see them.

## Edges

Edges are compared after node matching, in the `to` key space. A `from` edge whose two ends are matched (by key
or by a move) is looked up in `to` under the matched keys:

| Found? | Basis and attributes | Result |
|---|---|---|
| yes | equal | unchanged: counted (`unchanged_edges`), not a row |
| yes | different | `modified` (both refs) |
| no | — | `removed`; the `to` edges left over are `added` |

* All edge kinds are compared (the API will filter by kind).
* `undetermined` (one ref, `low`) replaces `removed`/`added` when the other snapshot could not show the edge:
  * an end is an undetermined node; or
  * an end's file was extracted on one side only. Its imports are invisible, or imports of it resolve
    differently (to the file instead of a declaration).
* A new import of a file that fails to parse on both sides is still `added`.
* An import into a moved file usually becomes `modified`, because its specifier is written differently
  (`.old.m` → `.new.m`). An import *out of* a moved file whose text is unchanged stays unchanged.

## Checks

`validate_against(before, after, comparison)`, which `compare` always runs, checks everything the models cannot
check alone:

* the run names the two snapshots;
* every node of each snapshot is in exactly one row, and every row names an existing node of its kind;
* a kind change is `modified`;
* a moved file has equal blobs, and a moved declaration moved with its file;
* every edge ref exists on its side;
* each snapshot's edges are accounted for exactly once (rows + `unchanged_edges`).

The models check the rest (SnapshotComparison, CIM §8): key presence by change kind, unique keys per side, the
comparability cap, and lineage.

## Orchestration

`xraymyrepo.orchestration.compare_snapshots(conn, from_snapshot_id, to_snapshot_id, params=None)` loads both
stored snapshots, runs `compare` and stores the result with `persist_comparison`. It returns the comparison id.

* **Checked first, before anything is loaded.** Both snapshots must exist (`SnapshotNotFoundError`) and be
  complete (`SnapshotNotCompleteError`). They must be two different snapshots of one repository
  (`SnapshotsNotComparableError`). If the identity `(from, to, "comparison", "1.0.0", params_hash)` is
  already stored in any status, the call raises `ComparisonExistsError` (with `comparison_id` and `status`)
  and computes nothing. Comparisons are deterministic, so a stored one is never recomputed or replaced.
* **Not fully comparable** snapshots (another extractor version or configuration) are not an error. The
  comparison is stored with its `incomparable` reasons and capped confidence.
* **Transactions.** Storing is one transaction (a savepoint inside the caller's). A failure leaves nothing
  behind, and the identity stays free. The snapshots are immutable, so loading them needs no transaction. Two
  concurrent calls for one identity store it once; the other raises `ComparisonExistsError`.
* **Direction matters.** `to → from` is a separate comparison.

There is no CLI.

## Fixture

`fixtures/comparison/v1/` holds two states of a small repository and the reviewed expected comparison (see its
`FIXTURE.md`).

## Limitations

* **No git history.** Direction is the caller's, and `git_rename` is not used. A file moved *and* edited is
  removed + added.
* **Python first.** With the default `move_languages`, non-Python files are never move-matched; files without
  declarations never are.
* **No declaration moves across files.** A function moved to another file is removed + added, even with an equal
  `content_hash`.
* **Containment.** A class's fingerprint covers its methods, so a method edit also modifies the class.
* **Endpoints across package renames** are removed + added (CIM §8).
* **Cost** (measured, no targets: `tests/api/test_large_comparison_db.py`, two ~8 100-node snapshots, three
  runs on one developer machine with local PostgreSQL 16; times vary about 2× between runs).
  * The pure comparison takes 0.36–0.74 s and storing it 0.29–0.38 s. The migration 0004 completion check
    in storing is 24–28 ms.
  * `compare_snapshots` takes 1.7–4.0 s, of which `load_snapshot` is 0.6–1.4 s per side.
  * API pages take 6–50 ms.
  * Loading both whole documents dominates orchestration; the documents are materialized in memory, as the
    algorithm requires.
* **Configuration changes** can add or remove nodes (for example a new ignore glob). The comparison is then
  marked `config_differs` and capped at `medium`, but each such node still reads as removed/added.
