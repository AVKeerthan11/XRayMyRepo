/**
 * API responses for tests, shaped after the backend's golden fixture. Every object is checked
 * against the types generated from docs/api/openapi-v1.json (`satisfies`), so a fixture can
 * never carry a field, endpoint or value the contract does not define.
 *
 * ``respond`` maps a request URL to a response; MSW (unit tests) and Playwright (e2e) share it.
 */
import type { Schemas } from "../api/client";

type S = Schemas;

const SHA_NEW = "0123456789abcdef0123456789abcdef01234567";
const SHA_OLD = "89abcdef0123456789abcdef0123456789abcdef";
const CONFIG_HASH = `sha256:${"ab".repeat(32)}`;
const PARAMS_HASH = `sha256:${"cd".repeat(32)}`;

export const GOLDEN_REF = { host: "github.com", owner: "xraymyrepo", name: "golden-fixture" } satisfies S["RepositoryRef"];
export const GOLDEN_SLUG = "github.com/xraymyrepo/golden-fixture";
export const EMPTY_SLUG = "github.com/acme/empty";

function snapshotSummary(id: number, sha: string, createdAt: string): S["SnapshotSummary"] {
  return {
    id,
    repository: GOLDEN_REF,
    commit_sha: sha,
    extractor_version: "0.2.0",
    config_hash: CONFIG_HASH,
    cim_schema_version: "1.0",
    status: "complete",
    failure_reason: null,
    created_at: createdAt,
    finished_at: createdAt,
  };
}

/** Snapshot 2 has architecture and health runs; snapshot 3 has none (directory fallback). */
export const SNAPSHOT_WITH_RUNS = snapshotSummary(2, SHA_NEW, "2026-10-12T09:30:00Z");
export const SNAPSHOT_WITHOUT_RUNS = snapshotSummary(3, SHA_OLD, "2026-10-01T15:00:00Z");
export const PENDING_SNAPSHOT_ID = 4;

export const REPOSITORIES = [
  {
    host: "github.com",
    owner: "xraymyrepo",
    name: "golden-fixture",
    slug: GOLDEN_SLUG,
    snapshot_count: 2,
    latest_snapshot: SNAPSHOT_WITH_RUNS,
  },
  { host: "github.com", owner: "acme", name: "empty", slug: EMPTY_SLUG, snapshot_count: 0, latest_snapshot: null },
] satisfies S["Repository"][];

function root(name: string): S["NodeSummary"] {
  return { key: "/", kind: "repository", name, parent_key: null, child_count: 4, language: null, extraction_status: null, tags: [] };
}

function snapshotDetail(summary: S["SnapshotSummary"]): S["SnapshotDetail"] {
  return {
    ...summary,
    config: {
      languages: ["python", "toml"],
      exclusion_policy: { source: "extract", generated: "extract", vendored: "file_only", docs: "file_only", build_artifact: "file_only" },
      ignore_globs: ["**/.git/**"],
    },
    producers: [{ name: "structure-extractor", version: "0.2.0", config_hash: CONFIG_HASH }],
    coverage: [{ language: "python", edge_kinds: ["IMPORTS"] }],
    root: root("golden-fixture"),
    stats: {
      nodes_by_kind: { repository: 1, directory: 9, file: 20, type: 6, function: 11, package: 2, external_package: 4 },
      edges_by_kind: { IMPORTS: 14 },
      unresolved_by_reason: { not_found: 1, dynamic: 1 },
      issues_by_severity: { error: 1, warning: 2 },
    },
  };
}

function run(id: number, algorithm: string, status: S["SnapshotStatus"], createdAt: string): S["DerivationRunSummary"] {
  return {
    id,
    snapshot_id: 2,
    algorithm,
    algorithm_version: "1.0.0",
    params_hash: PARAMS_HASH,
    status,
    failure_reason: status === "failed" ? "derivation failed" : null,
    created_at: createdAt,
    finished_at: status === "pending" ? null : createdAt,
  };
}

/** Newest first, any status, as the API lists them. Run 12 (pending) and 9 (failed) must be ignored. */
export const RUNS = [
  run(12, "architecture", "pending", "2026-10-12T11:00:00Z"),
  run(11, "health", "complete", "2026-10-12T10:05:00Z"),
  run(10, "architecture", "complete", "2026-10-12T10:00:00Z"),
  run(9, "architecture", "failed", "2026-10-12T09:45:00Z"),
] satisfies S["DerivationRunSummary"][];

export const ARCHITECTURE_RUN = {
  ...run(10, "architecture", "complete", "2026-10-12T10:00:00Z"),
  params: {},
  finding_counts: { "arch.layer.violation": 1 },
  lenses: [
    { name: "directory", kind: "directory", group_count: 7, member_count: 20 },
    { name: "components", kind: "inferred", group_count: 3, member_count: 12 },
  ],
  layer_coverage: { considered: 12, classified: 9, unclassified: 3, by_layer: { interface: 3, application: 2, domain: 2, infrastructure: 2 }, by_confidence: { high: 6, medium: 3 } },
} satisfies S["DerivationRunDetail"];

const PROJECT_KEY = "group:components:pkg:pypi:acme-backend";
export const APP_KEY = `${PROJECT_KEY}#app`;
export const USERS_KEY = `${PROJECT_KEY}#app.users`;

function group(key: string, label: string, parent: string | null, extra: Partial<S["GroupSummary"]>): S["GroupSummary"] {
  return {
    key,
    label,
    lens: "components",
    parent_key: parent,
    member_count: 0,
    child_group_count: 0,
    metrics: {},
    layer_mix: { by_layer: {}, unclassified: 0 },
    basis: "inferred",
    confidence: "high",
    ...extra,
  };
}

export const PROJECT_GROUP = group(PROJECT_KEY, "acme-backend", null, {
  child_group_count: 2,
  metrics: { files: 9, internal_edges: 6, afferent: 0, efferent: 0 },
  layer_mix: { by_layer: { interface: 3, application: 2, domain: 2, infrastructure: 1 }, unclassified: 1 },
});
export const APP_GROUP = group(APP_KEY, "app", PROJECT_KEY, {
  member_count: 2,
  metrics: { files: 2, internal_edges: 0, afferent: 3, efferent: 0, instability: 0 },
  layer_mix: { by_layer: { infrastructure: 1 }, unclassified: 1 },
});
export const USERS_GROUP = group(USERS_KEY, "app.users", PROJECT_KEY, {
  member_count: 3,
  metrics: { files: 3, internal_edges: 3, afferent: 1, efferent: 2, instability: 0.6666666666666666 },
  layer_mix: { by_layer: { interface: 2, application: 1 }, unclassified: 0 },
  confidence: "medium",
});
export const TESTS_GROUP = group(`group:components:/#tests`, "tests", null, { member_count: 1, metrics: { files: 1 } });
export const SCRIPTS_GROUP = group(`group:components:/#manage`, "manage", null, {
  member_count: 1,
  metrics: { files: 1 },
  confidence: "low",
});

function file(key: string, extra: Partial<S["NodeSummary"]> = {}): S["NodeSummary"] {
  const name = key.slice(key.lastIndexOf("/") + 1);
  return {
    key,
    kind: "file",
    name,
    parent_key: key.includes("/") ? key.slice(0, key.lastIndexOf("/")) : "/",
    child_count: 1,
    language: name.endsWith(".py") ? "python" : null,
    extraction_status: name.endsWith(".py") ? "full" : "unsupported",
    tags: [{ facet: "origin", value: "source" }],
    ...extra,
  };
}

export const SERVICE_KEY = "backend/app/users/service.py";
const USERS_FILES = [file("backend/app/users/models.py"), file("backend/app/users/routes.py"), file(SERVICE_KEY)];
const APP_FILES = [file("backend/app/db.py"), file("backend/app/legacy.py", { extraction_status: "failed", child_count: 0 })];

function directory(key: string, childCount: number): S["NodeSummary"] {
  return { ...file(key), kind: "directory", child_count: childCount, language: null, extraction_status: null, tags: [] };
}

/** The directory fallback (snapshot 3): the root's children, and backend's. */
export const TREE: Record<string, S["NodeSummary"][]> = {
  "/": [directory("backend", 2), directory("frontend", 3), file("README.md", { child_count: 0, tags: [{ facet: "origin", value: "docs" }] })],
  backend: [directory("backend/app", 6), file("backend/pyproject.toml", { language: "toml", extraction_status: "full", child_count: 0 })],
};

export function nodeDetail(summary: S["NodeSummary"]): S["NodeDetail"] {
  const isService = summary.key === SERVICE_KEY;
  return {
    ...summary,
    ancestors: [{ key: "/", kind: "repository", name: "golden-fixture" }],
    attributes: summary.kind === "file" && summary.language === "python" ? { module_name: summary.key.replace(/^backend\//, "").replace(/\.py$/, "").replaceAll("/", ".") } : {},
    basis: "observed",
    confidence: "high",
    provenance: { producer: "structure-extractor", rule: "fs.tree" },
    evidence: [],
    classifications: [],
    blob_sha: summary.kind === "file" ? SHA_OLD : null,
    content_hash: null,
    signature_hash: null,
    span: null,
    relationship_counts: isService ? { IMPORTS: { incoming: 1, outgoing: 2 } } : {},
    diagnostic_counts: { extraction_issues: summary.extraction_status === "failed" ? 1 : 0, unresolved_references: 0 },
  };
}

function problem(status: number, code: string, title: string, detail: string, instance: string): S["Problem"] {
  return { type: `urn:xraymyrepo:problem:${code}`, title, status, detail, code, instance };
}

function page<T>(items: T[]): { items: T[]; next_cursor: string | null } {
  return { items, next_cursor: null };
}

export interface FixtureResponse {
  status: number;
  body: unknown;
}

/** The fixture response for an API request URL, or a 404 problem for anything unknown. */
export function respond(url: URL): FixtureResponse {
  const path = url.pathname;
  const q = url.searchParams;
  const ok = (body: unknown): FixtureResponse => ({ status: 200, body });
  const notFound = (code = "not_found", detail = "Not found."): FixtureResponse => ({
    status: 404,
    body: problem(404, code, "Not found", detail, path),
  });

  if (path === "/api/v1/repositories") return ok(page(REPOSITORIES));
  if (path === `/api/v1/repositories/${GOLDEN_SLUG}/snapshots`) return ok(page([SNAPSHOT_WITH_RUNS, SNAPSHOT_WITHOUT_RUNS]));
  if (path === `/api/v1/repositories/${EMPTY_SLUG}/snapshots`) return ok(page([]));

  const snapshot = /^\/api\/v1\/snapshots\/(\d+)(\/.*)?$/.exec(path);
  if (snapshot) {
    const id = Number(snapshot[1]);
    const rest = snapshot[2] ?? "";
    if (id === PENDING_SNAPSHOT_ID) {
      return { status: 409, body: problem(409, "snapshot_not_complete", "Snapshot not complete", `Snapshot ${id} is pending.`, path) };
    }
    const summary = id === 2 ? SNAPSHOT_WITH_RUNS : id === 3 ? SNAPSHOT_WITHOUT_RUNS : null;
    if (summary === null) return notFound("snapshot_not_found", `Snapshot ${id} does not exist.`);
    if (rest === "") return ok(snapshotDetail(summary));
    if (rest === "/derivations") return ok(page(id === 2 ? RUNS : []));
    if (rest === "/tree/children") {
      const children = TREE[q.get("parent") ?? "/"];
      return children ? ok(page(children)) : ok(page([]));
    }
    if (rest === "/nodes/by-key") {
      const key = q.get("key") ?? "";
      const known = [...USERS_FILES, ...APP_FILES, ...Object.values(TREE).flat()].find((n) => n.key === key);
      return known ? ok(nodeDetail(known)) : notFound("node_not_found", `Node ${key} does not exist.`);
    }
    return notFound();
  }

  const derivation = /^\/api\/v1\/derivations\/(\d+)(\/.*)?$/.exec(path);
  if (derivation) {
    if (Number(derivation[1]) !== 10) return notFound("derivation_not_found", `Derivation run ${derivation[1]} does not exist.`);
    const rest = derivation[2] ?? "";
    if (rest === "") return ok(ARCHITECTURE_RUN);
    if (rest === "/groups/children") {
      if (q.get("lens") !== "components") return ok(page([]));
      const parent = q.get("parent");
      if (parent === null) return ok(page([PROJECT_GROUP, TESTS_GROUP, SCRIPTS_GROUP]));
      return ok(page(parent === PROJECT_KEY ? [APP_GROUP, USERS_GROUP] : []));
    }
    if (rest === "/groups/members") {
      const members: Record<string, S["NodeSummary"][]> = { [APP_KEY]: APP_FILES, [USERS_KEY]: USERS_FILES };
      return ok(page(members[q.get("group") ?? ""] ?? []));
    }
    return notFound();
  }
  return notFound();
}
