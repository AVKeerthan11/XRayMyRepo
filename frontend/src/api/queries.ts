import { useQuery } from "@tanstack/react-query";

import { api, type Schemas } from "./client";
import { unwrap } from "./errors";
import { collectPages } from "./paginate";

/** Upper bounds on what one view pulls into the browser; a truncated list says so. */
export const LIMITS = {
  repositories: 1000,
  snapshots: 200,
  derivations: 200,
  outlineLevel: 500,
} as const;

/**
 * Completed snapshots and their derivations never change (immutability is enforced by the
 * database), so their responses stay fresh for the whole session.
 */
const IMMUTABLE = { staleTime: Number.POSITIVE_INFINITY, gcTime: 30 * 60_000 } as const;

export const queryKeys = {
  repositories: ["repositories"] as const,
  snapshots: (slug: string) => ["snapshots", slug] as const,
  snapshot: (id: number) => ["snapshot", id] as const,
  derivations: (id: number) => ["derivations", id] as const,
  run: (id: number) => ["run", id] as const,
  groupChildren: (run: number, lens: string, parent: string | null) => ["group-children", run, lens, parent] as const,
  groupMembers: (run: number, group: string) => ["group-members", run, group] as const,
  treeChildren: (snapshot: number, parent: string) => ["tree-children", snapshot, parent] as const,
  node: (snapshot: number, key: string) => ["node", snapshot, key] as const,
};

export function useRepositories() {
  return useQuery({
    queryKey: queryKeys.repositories,
    queryFn: () =>
      collectPages(
        (cursor, limit) => unwrap(api.GET("/api/v1/repositories", { params: { query: { limit, cursor } } })),
        LIMITS.repositories,
      ),
  });
}

/** Complete snapshots of a repository, newest first (the API's default `status=complete`). */
export function useSnapshots(repository: Schemas["RepositoryRef"] | null) {
  return useQuery({
    queryKey: queryKeys.snapshots(repository ? slugOf(repository) : ""),
    enabled: repository !== null,
    queryFn: () => {
      const path = repository as Schemas["RepositoryRef"];
      return collectPages(
        (cursor, limit) =>
          unwrap(
            api.GET("/api/v1/repositories/{host}/{owner}/{name}/snapshots", {
              params: { path: { host: path.host, owner: path.owner, name: path.name }, query: { limit, cursor } },
            }),
          ),
        LIMITS.snapshots,
      );
    },
  });
}

export function useSnapshot(snapshotId: number) {
  return useQuery({
    queryKey: queryKeys.snapshot(snapshotId),
    queryFn: () => unwrap(api.GET("/api/v1/snapshots/{snapshot_id}", { params: { path: { snapshot_id: snapshotId } } })),
    ...IMMUTABLE,
  });
}

/** Derivation runs of a snapshot, in any status. Not immutable as a list: new runs may appear. */
export function useDerivations(snapshotId: number, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.derivations(snapshotId),
    enabled,
    queryFn: () =>
      collectPages(
        (cursor, limit) =>
          unwrap(
            api.GET("/api/v1/snapshots/{snapshot_id}/derivations", {
              params: { path: { snapshot_id: snapshotId }, query: { limit, cursor } },
            }),
          ),
        LIMITS.derivations,
      ),
  });
}

export function useDerivationRun(runId: number | null) {
  return useQuery({
    queryKey: queryKeys.run(runId ?? 0),
    enabled: runId !== null,
    queryFn: () => unwrap(api.GET("/api/v1/derivations/{run_id}", { params: { path: { run_id: runId as number } } })),
    ...IMMUTABLE,
  });
}

/** A lens's top-level groups (``parent`` null) or one group's child groups. */
export function useGroupChildren(runId: number, lens: string, parent: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.groupChildren(runId, lens, parent),
    enabled,
    queryFn: () =>
      collectPages(
        (cursor, limit) =>
          unwrap(
            api.GET("/api/v1/derivations/{run_id}/groups/children", {
              params: { path: { run_id: runId }, query: { lens, parent: parent ?? undefined, limit, cursor } },
            }),
          ),
        LIMITS.outlineLevel,
      ),
    ...IMMUTABLE,
  });
}

/** Files assigned directly to a group. */
export function useGroupMembers(runId: number, group: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.groupMembers(runId, group),
    enabled,
    queryFn: () =>
      collectPages(
        (cursor, limit) =>
          unwrap(
            api.GET("/api/v1/derivations/{run_id}/groups/members", {
              params: { path: { run_id: runId }, query: { group, limit, cursor } },
            }),
          ),
        LIMITS.outlineLevel,
      ),
    ...IMMUTABLE,
  });
}

/** Directories and files directly under ``parent`` (the repository root is ``/``). */
export function useTreeChildren(snapshotId: number, parent: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.treeChildren(snapshotId, parent),
    enabled,
    queryFn: () =>
      collectPages(
        (cursor, limit) =>
          unwrap(
            api.GET("/api/v1/snapshots/{snapshot_id}/tree/children", {
              params: {
                path: { snapshot_id: snapshotId },
                query: { parent, kind: ["directory", "file"], limit, cursor },
              },
            }),
          ),
        LIMITS.outlineLevel,
      ),
    ...IMMUTABLE,
  });
}

export function useNodeDetail(snapshotId: number, key: string | null) {
  return useQuery({
    queryKey: queryKeys.node(snapshotId, key ?? ""),
    enabled: key !== null,
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/snapshots/{snapshot_id}/nodes/by-key", {
          params: { path: { snapshot_id: snapshotId }, query: { key: key as string } },
        }),
      ),
    ...IMMUTABLE,
  });
}

export function slugOf(repository: Schemas["RepositoryRef"]): string {
  return `${repository.host}/${repository.owner}/${repository.name}`;
}
