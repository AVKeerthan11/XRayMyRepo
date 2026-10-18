# Ingestion v1: a public GitHub repository at one commit → a persisted snapshot

`xraymyrepo.ingestion` turns a GitHub URL plus a commit SHA or ref into an immutable CIM snapshot in
PostgreSQL, using the existing analyzer and `persist_snapshot` unchanged. There is no HTTP write endpoint;
results are read through the existing read-only API (`GET /api/v1/snapshots/{id}`, …).

```
github.ingestion_target ─▶ git.resolve_commit ─▶ find_snapshot ─(stored)─▶ result
                                                       │
                                                       └─(new)─▶ git.retrieve ─▶ analyze() ─▶ persist_snapshot ─▶ result
```

```python
import psycopg
from xraymyrepo.ingestion.service import ingest

with psycopg.connect(url, autocommit=True) as conn:
    result = ingest(conn, "https://github.com/acme/app", ref="main")
    result.snapshot_id, result.commit_sha, result.already_existed, result.skipped
```

## Command line

```
export XRAY_DATABASE_URL=postgresql://user:pass@localhost:5432/xray   # migrated with db/migrations
xray ingest https://github.com/acme/app --ref main
xray ingest https://github.com/acme/app --commit-sha 0123456789abcdef0123456789abcdef01234567
python -m xraymyrepo ingest ...                                       # the same, without the script
```

Install with `pip install -e .[postgres]` (the `xray` script now points at `xraymyrepo.cli:main`; reinstall
an existing editable install to pick it up). `xray analyze` is unchanged and needs no database driver.

* **Database:** `XRAY_DATABASE_URL`, the variable the API uses, naming a database migrated with
  `db/migrations`. It is read only from the environment (never an argument: process lists and shell history
  would expose a password) and is never printed, not even when the connection fails.
* **Target:** the URL plus exactly one of `--ref` (branch, tag, `refs/...` or `HEAD`, resolved to a commit)
  or `--commit-sha` (full SHA). Pass refs starting with `-` as `--ref=-x`; they are rejected anyway.
* **Output:** one line of JSON on stdout:

  ```json
  {"snapshot_id": 7, "repository": {"host": "github.com", "owner": "acme", "name": "app"},
   "commit_sha": "…", "extractor_version": "0.2.0", "config_hash": "sha256:…",
   "already_existed": false, "skipped": [{"path": "docs", "reason": "symlink"}]}
  ```

  `skipped` is `[]` when nothing was skipped and `null` when the snapshot already existed (unknown to that
  run). Skipped entries are also summarized on stderr: `xray ingest: note: 1 tree entries are not represented
  in the snapshot: symlink 'docs'`.
* **Errors:** stdout stays empty; stderr has one line `xray ingest: <code>: <message>`. Messages never
  include the database URL, the repository URL as given (it may carry a token), git's own output or local
  temporary paths; unexpected errors print only their type.

| Exit | Code | Meaning |
|---|---|---|
| 0 | | ingested, or the snapshot already existed |
| 1 | `unexpected_error` | anything else (details not printed) |
| 2 | (usage), `invalid_input`, `configuration` | bad arguments, URL, SHA or ref; `XRAY_DATABASE_URL` not set |
| 3 | `retrieval_failed` | git unavailable; repository unavailable or private; ref or commit not found; unsafe tree |
| 4 | `limit_exceeded`, `timeout` | a `RetrievalLimits` limit, or the retrieval deadline |
| 5 | `analysis_failed` | the analyzer failed |
| 6 | `database_unavailable`, `database_error` | cannot connect; letter-case conflict; incomplete snapshot with this identity; other database errors |

Safe use: run it with a database role that may write only this schema, against public repositories you
intend to store; retrieval limits are the defaults of `RetrievalLimits` (not configurable from the CLI).

## Modules

| Module | Role | Needs |
|---|---|---|
| `ingestion.github` | URL, commit SHA and ref validation (pure) | nothing |
| `ingestion.git` | `resolve_commit`, `retrieve`: one commit into a temporary directory, under `RetrievalLimits` | `git` |
| `ingestion.service` | `ingest`: validate → resolve → analyze → persist | `git`, `psycopg` |
| `ingestion.cli` | `xray ingest` | `git`, `psycopg` |

`ingestion` imports the analyzer and persistence; nothing below it, nor the API, imports it
(`tests/test_architecture.py`).

## Identity and repeated ingestion

* The snapshot identity is the existing one: `(github.com/<owner>/<name>, commit SHA, ANALYZER_VERSION,
  config hash of the default AnalysisOptions)`. Owner and name are lower-cased (GitHub treats them
  case-insensitively), so every spelling of a URL gives one identity. A ref is resolved to a commit
  first; only the commit identifies the snapshot, so a branch and its commit SHA give the same snapshot.
* **Already stored:** after resolving the commit, `find_snapshot` is checked. A complete snapshot is
  returned (`already_existed=True`) without fetching or analyzing anything.
* **Concurrent ingestion:** if another ingestion persists the identity between that check and
  `persist_snapshot`, persistence raises `SnapshotExistsError`; a *complete* existing snapshot is returned
  as `already_existed=True`. A pending or failed one is raised, never reported as success. Every other
  database, analyzer or retrieval error propagates unchanged.
* **Letter case:** persistence matches repositories ignoring case and rejects a second spelling
  (`RepositoryIdentityError`). Ingestion checks this before retrieving, so a repository stored as
  `github.com/Acme/App` by another producer fails fast instead of after the analysis.
* No migration: ingestion stores nothing beyond the snapshot.

## What the snapshot does not represent

Retrieval writes only regular files (modes `100644`, `100755`). **Symlinks (`120000`) and submodules
(`160000`) are skipped**: the analyzer never sees them, so the snapshot has no file node and no extraction
issue for them. (The analyzer reports `symlink_skipped` only for links it finds on disk, and retrieval
writes none.) The CIM document does not record the omission, and the contract is unchanged; instead
`IngestResult.skipped` lists the skipped entries, in tree order, for the ingestion that retrieved the commit.
When the snapshot already existed, `skipped` is `None`: the omissions are unknown to that call, not absent.

Everything else a commit holds is either represented or fails the ingestion: a file over
`max_file_bytes`, more than `max_files` entries, unsafe or non-portable paths, and case collisions are
errors, never silent omissions (see `RetrievalLimits` and the `ingestion.git` docstring).

## Limits and safety

See `src/xraymyrepo/ingestion/git.py`: fixed `https://github.com/<owner>/<name>.git` remote, sanitized git
environment and configuration, shallow and blob-filtered fetch, verified blob streaming, per-command
timeouts and one overall deadline (`RetrievalLimits.deadline`, taken once per ingestion and shared by
resolving the ref, fetching, tree inspection and materialization; analysis and persistence are not timed),
and removal of the temporary directory on every exit. The checkout is removed before the snapshot is
persisted.

## Tests

`tests/ingestion/` uses local bare repositories as the remote; nothing contacts GitHub unless
`XRAY_TEST_GITHUB=1`. `test_service.py` and `test_cli.py` cover the service and the command against an
in-memory store; `test_service_db.py` needs `XRAY_TEST_DATABASE_URL` (see docs/persistence/postgres-v1.md)
and also reads the ingested snapshot back through the API and runs the command against the database.
