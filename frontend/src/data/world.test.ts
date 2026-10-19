import { describe, expect, it } from "vitest";

import { ARCHITECTURE_RUN, RUNS, TREE, USERS_GROUP, USERS_KEY } from "../test/fixtures";
import { ARCHITECTURE, HEALTH, chooseIslandSource, newestCompleteRun, selectionKey } from "./world";

describe("newestCompleteRun", () => {
  it("ignores pending and failed runs", () => {
    expect(newestCompleteRun(RUNS, ARCHITECTURE)?.id).toBe(10); // 12 is pending, 9 failed
    expect(newestCompleteRun(RUNS, HEALTH)?.id).toBe(11);
  });

  it("picks the newest complete run whatever the listing order", () => {
    const older = { ...RUNS[2]!, id: 5, created_at: "2026-01-01T00:00:00Z" };
    expect(newestCompleteRun([older, RUNS[2]!], ARCHITECTURE)?.id).toBe(10);
    expect(newestCompleteRun([RUNS[2]!, older], ARCHITECTURE)?.id).toBe(10);
  });

  it("returns null when there is no complete run", () => {
    expect(newestCompleteRun([], ARCHITECTURE)).toBeNull();
    expect(newestCompleteRun([RUNS[0]!, RUNS[3]!], ARCHITECTURE)).toBeNull();
  });
});

describe("chooseIslandSource", () => {
  it("prefers an inferred lens with groups", () => {
    expect(chooseIslandSource(ARCHITECTURE_RUN)).toEqual({ kind: "lens", runId: 10, lens: ARCHITECTURE_RUN.lenses[1] });
  });

  it("falls back to the directory lens when the inferred one is empty", () => {
    const run = { ...ARCHITECTURE_RUN, lenses: ARCHITECTURE_RUN.lenses.map((l) => (l.kind === "inferred" ? { ...l, group_count: 0 } : l)) };
    expect(chooseIslandSource(run)).toMatchObject({ kind: "lens", lens: { name: "directory" } });
  });

  it("falls back to top-level directories without a run or without groups", () => {
    expect(chooseIslandSource(null)).toEqual({ kind: "directories", reason: "no_architecture_run" });
    expect(chooseIslandSource({ ...ARCHITECTURE_RUN, lenses: [] })).toEqual({ kind: "directories", reason: "no_groups" });
  });
});

describe("selectionKey", () => {
  it("is the selected group's or node's key", () => {
    expect(selectionKey(null)).toBeNull();
    expect(selectionKey({ type: "group", group: USERS_GROUP })).toBe(USERS_KEY);
    expect(selectionKey({ type: "node", node: TREE["/"]![0]! })).toBe("backend");
  });
});
