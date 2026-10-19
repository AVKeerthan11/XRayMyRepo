import type { Schemas } from "../api/client";

/** Derivation algorithms the world is built from (`ALGORITHM` in the backend). */
export const ARCHITECTURE = "architecture";
export const HEALTH = "health";

type Run = Schemas["DerivationRunSummary"];
type Lens = Schemas["LensSummary"];

/**
 * The newest complete run of ``algorithm``, or null. The API lists runs newest first in any
 * status and has no "latest" shortcut, so the client chooses; pending and failed runs are
 * never used.
 */
export function newestCompleteRun(runs: readonly Run[], algorithm: string): Run | null {
  let best: Run | null = null;
  for (const run of runs) {
    if (run.algorithm !== algorithm || run.status !== "complete") continue;
    if (best === null || run.created_at > best.created_at || (run.created_at === best.created_at && run.id > best.id)) {
      best = run;
    }
  }
  return best;
}

/** Where the world's islands come from. */
export type IslandSource =
  /** Top-level groups of a lens of an architecture run. */
  | { kind: "lens"; runId: number; lens: Lens }
  /** No usable architecture run: the repository's top-level directories. */
  | { kind: "directories"; reason: "no_architecture_run" | "no_groups" };

/** Lens kinds in order of preference: inferred components read as architecture, directories as layout. */
const LENS_PREFERENCE: readonly Lens["kind"][] = ["inferred", "package", "directory"];

export function chooseIslandSource(run: Schemas["DerivationRunDetail"] | null): IslandSource {
  if (run === null) return { kind: "directories", reason: "no_architecture_run" };
  for (const kind of LENS_PREFERENCE) {
    const lens = run.lenses.find((l) => l.kind === kind && l.group_count > 0);
    if (lens) return { kind: "lens", runId: run.id, lens };
  }
  return { kind: "directories", reason: "no_groups" };
}

/** The selected object of the world, inspected in the panel. */
export type Selection =
  | { type: "group"; group: Schemas["GroupSummary"] }
  | { type: "node"; node: Schemas["NodeSummary"] };

export function selectionKey(selection: Selection | null): string | null {
  if (selection === null) return null;
  return selection.type === "group" ? selection.group.key : selection.node.key;
}
