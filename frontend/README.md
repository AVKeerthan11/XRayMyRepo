# XRayMyRepo frontend

The explorable world of a codebase, built from one immutable snapshot served by the read-only
XRayMyRepo API. **Milestone S0:** the application shell, repository and snapshot selection,
`/world/:snapshotId` with real API data, and the accessible 2D world outline that remains the
fallback of the 3D world (S1).

Stack: React, TypeScript, Vite, Tailwind CSS, TanStack Query, and a client typed from the backend's
contract (`../docs/api/openapi-v1.json` → `src/api/schema.d.ts`). No 3D libraries yet.

## Develop

Requires Node 20.19+ (tested with 24) and the API running locally.

```sh
npm install
npm run dev            # http://localhost:5173, proxies /api to http://127.0.0.1:8000
```

`/api` is proxied by Vite (`vite.config.ts`), so the browser stays same-origin and the API needs no
`XRAY_CORS_ORIGINS`. Set `XRAY_API_URL` to proxy somewhere else.

| Command | What it does |
|---|---|
| `npm run dev` | Dev server with the `/api` proxy |
| `npm run build` | Type-check and production build (`dist/`) |
| `npm run typecheck` | `tsc --noEmit` over app, tests and configs |
| `npm run gen:api` | Regenerate `src/api/schema.d.ts` from `../docs/api/openapi-v1.json` |
| `npm run check:api` | Fail if `schema.d.ts` no longer matches the OpenAPI contract |
| `npm test` | Unit and component tests (Vitest, Testing Library, MSW) |
| `npm run e2e` | End-to-end tests in Chromium, desktop and Pixel 7 (Playwright, axe) |

The first `npm run e2e` needs a browser: `npx playwright install chromium`.

## Tests and fixtures

Tests never need the API or a database. `src/test/fixtures.ts` holds responses shaped after the
backend's golden fixture, each checked against the generated contract types with `satisfies`, so a
fixture cannot carry a field or endpoint the API does not have. One pure function, `respond(url)`,
serves them to MSW (unit tests) and to Playwright (`page.route`, e2e).

End-to-end tests cover the exploration flow, keyboard-only use, axe (WCAG 2.1 A/AA, no serious or
critical violations), 44 px touch targets and no sideways scrolling on mobile, the WebGL2-unavailable
fallback, reduced motion, and an unreachable API.

## Preparing real data

The world reads what the backend has stored; nothing here writes to the database. A snapshot needs an
`architecture` derivation run for islands (without one, islands are top-level directories) and a `health`
run for S1's building heights. Both are created with the backend's existing orchestration functions.
Use the dedicated application database `xray`, **never `xraytest` or `xraytest_comparison`**: those are
disposable test databases, and the backend test suite drops and recreates the schema of the one it uses.

PowerShell (Windows PowerShell 5.1 or later), from the repository root, with the `xray-postgres` container
running and the backend's virtual environment active:

```powershell
# 1. The application database, migrated once. Migrations are applied by the caller, in filename order;
#    each file is one transaction and records itself in schema_migration. psql reads every file inside
#    the container and stops at the first error.
docker exec xray-postgres createdb -U postgres xray
docker cp db/migrations/. xray-postgres:/tmp/xray-migrations
foreach ($f in Get-ChildItem db/migrations -Filter *.sql | Sort-Object Name) {
  docker exec -e PGCLIENTENCODING=UTF8 xray-postgres psql -U postgres -d xray -X -q -v ON_ERROR_STOP=1 -f "/tmp/xray-migrations/$($f.Name)"
  if ($LASTEXITCODE -ne 0) { Write-Error "Migration $($f.Name) failed; stopping."; break }
}
docker exec xray-postgres psql -U postgres -d xray -X -c "SELECT version FROM schema_migration ORDER BY version"

# 2. Point the backend at it, in this shell only. The password is read from the container into a
#    variable: it is never typed, printed, or saved in history or files.
$pw = "$(docker exec xray-postgres printenv POSTGRES_PASSWORD)".Trim()
if (-not $pw) { throw "POSTGRES_PASSWORD is not set in xray-postgres" }
$env:XRAY_DATABASE_URL = "postgresql://postgres:" + [uri]::EscapeDataString($pw) + "@127.0.0.1:5432/xray"
Remove-Variable pw

# 3. Ingest a public repository; note "snapshot_id" in the JSON it prints.
python -m xraymyrepo ingest https://github.com/pallets/flask --ref main

# 4. Derive architecture and health for that snapshot (replace 1 with the snapshot_id).
python -c "import os, psycopg; from xraymyrepo.orchestration import derive_architecture, derive_health; conn = psycopg.connect(os.environ['XRAY_DATABASE_URL'], autocommit=True); print(derive_architecture(conn, 1), derive_health(conn, 1))"

# 5. Serve the API on 127.0.0.1:8000, then run the frontend (npm run dev) in another shell.
uvicorn --factory xraymyrepo.api:create_app
```

Re-running step 4 raises `DerivationRunExistsError` by design: runs are immutable and never replaced.

Why these commands look the way they do:

* **No `sh -c` loop.** Windows PowerShell 5.1 drops embedded double quotes when it passes arguments to
  `docker`, so a `sh -c '… "$f" …'` script reaches `sh` split apart and fails with
  `Syntax error: end of file unexpected (expecting "done")`. The loop runs in PowerShell instead, one
  `docker exec … psql -f` per file. SQL is never piped from PowerShell into `psql` (PowerShell re-encodes
  piped text).
* **`127.0.0.1`, not `localhost`.** `localhost` may resolve to `::1`, where another process (for example
  a WSL relay) can be listening on port 5432 instead of the container.
* **Steps 2 and 5 share a shell.** `XRAY_DATABASE_URL` is set for the current PowerShell session only; set
  it again in any new shell that runs ingestion, derivations or the API.

## Layout

```
src/api/        generated contract types, typed client, errors, bounded pagination, query hooks
src/data/       world rules: which runs to use, where islands come from
src/platform/   WebGL2 and reduced-motion detection
src/routes/     Home (observatory), World, NotFound
src/ui/         shell, states, breadcrumbs, world outline (2D fallback), inspection panel
src/styles/     Tailwind and design tokens
src/test/       contract-typed fixtures, MSW server, render helper
e2e/            Playwright specs
scripts/        schema drift check
```
