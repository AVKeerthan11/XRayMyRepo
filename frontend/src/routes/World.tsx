import { useState } from "react";
import { Link, useParams } from "react-router";

import type { Schemas } from "../api/client";
import { useDerivationRun, useDerivations, useGroupChildren, useSnapshot, slugOf } from "../api/queries";
import {
  ARCHITECTURE,
  HEALTH,
  chooseIslandSource,
  newestCompleteRun,
  selectionKey,
  type IslandSource,
  type Selection,
} from "../data/world";
import { useReducedMotion, useWebGL2 } from "../platform/capabilities";
import { Breadcrumbs } from "../ui/Breadcrumbs";
import { formatTimestamp, plural, shortSha } from "../ui/format";
import { InspectionPanel } from "../ui/InspectionPanel";
import { Command, EmptyState, ErrorState, Eyebrow, Loading } from "../ui/Status";
import { WorldOutline } from "../ui/WorldOutline";

/** A positive integer path segment, or null (`/world/abc`, `/world/0`). */
export function parseSnapshotId(raw: string | undefined): number | null {
  if (raw === undefined || !/^[1-9]\d{0,15}$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) ? id : null;
}

export function World() {
  const { snapshotId: raw } = useParams();
  const snapshotId = parseSnapshotId(raw);
  if (snapshotId === null) {
    return (
      <div className="mx-auto max-w-3xl">
        <EmptyState title="That is not a snapshot">
          <p>
            World addresses look like <span className="font-mono">/world/42</span>.{" "}
            <Link to="/" className="text-signal underline underline-offset-4">
              Back to the observatory
            </Link>
          </p>
        </EmptyState>
      </div>
    );
  }
  return <WorldView key={snapshotId} snapshotId={snapshotId} />;
}

function WorldView({ snapshotId }: { snapshotId: number }) {
  const snapshot = useSnapshot(snapshotId);
  const derivations = useDerivations(snapshotId, snapshot.isSuccess);
  const runs = derivations.data?.items ?? [];
  const architecture = derivations.isSuccess ? newestCompleteRun(runs, ARCHITECTURE) : null;
  const health = derivations.isSuccess ? newestCompleteRun(runs, HEALTH) : null;
  const architectureDetail = useDerivationRun(architecture?.id ?? null);
  const [selection, setSelection] = useState<Selection | null>(null);

  if (snapshot.isPending) {
    return (
      <div className="mx-auto max-w-6xl">
        <Loading label={`Charting snapshot ${snapshotId}…`} />
      </div>
    );
  }
  if (snapshot.isError) {
    return (
      <div className="mx-auto max-w-3xl">
        <ErrorState error={snapshot.error} onRetry={() => void snapshot.refetch()} />
        <p className="mt-6 text-sm">
          <Link to="/" className="text-signal underline underline-offset-4">
            Back to the observatory
          </Link>
        </p>
      </div>
    );
  }

  const detail = snapshot.data;
  const slug = slugOf(detail.repository);
  let source: IslandSource | null = null;
  if (derivations.isSuccess && (architecture === null || architectureDetail.isSuccess)) {
    source = chooseIslandSource(architecture === null ? null : (architectureDetail.data ?? null));
  }

  return (
    <div className="mx-auto max-w-6xl">
      <Breadcrumbs
        crumbs={[
          { label: "Observatory", to: "/" },
          { label: slug, to: `/?repository=${encodeURIComponent(slug)}`, mono: true },
          { label: shortSha(detail.commit_sha), mono: true },
        ]}
      />
      <SnapshotHeader detail={detail} />
      <Stage />

      <section aria-labelledby="sources-heading" className="mt-10">
        <Eyebrow id="sources-heading">World sources</Eyebrow>
        <div className="mt-3">
          {derivations.isPending ? (
            <Loading label="Looking for derivation runs…" />
          ) : derivations.isError ? (
            <ErrorState error={derivations.error} onRetry={() => void derivations.refetch()} />
          ) : (
            <RunSources architecture={architecture} health={health} snapshotId={snapshotId} />
          )}
        </div>
      </section>

      <div className="mt-12 grid gap-12 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:gap-16">
        <section aria-labelledby="outline-heading">
          <h2 id="outline-heading" className="text-xs font-medium tracking-[0.2em] text-faint uppercase">
            World outline
          </h2>
          <div className="mt-4">
            {architectureDetail.isError ? (
              <ErrorState error={architectureDetail.error} onRetry={() => void architectureDetail.refetch()} />
            ) : source === null ? (
              derivations.isError ? null : <Loading label="Charting islands…" />
            ) : (
              <>
                <SourceNote source={source} />
                <div className="mt-4 border-y border-line">
                  <WorldOutline
                    snapshotId={snapshotId}
                    source={source}
                    selectedKey={selectionKey(selection)}
                    onSelect={setSelection}
                  />
                </div>
              </>
            )}
          </div>
        </section>
        <InspectionPanel snapshotId={snapshotId} selection={selection} />
      </div>
    </div>
  );
}

const KIND_LABELS: readonly [string, string, string][] = [
  ["file", "file", "files"],
  ["directory", "directory", "directories"],
  ["type", "type", "types"],
  ["function", "function", "functions"],
  ["package", "package", "packages"],
  ["external_package", "external package", "external packages"],
];

function SnapshotHeader({ detail }: { detail: Schemas["SnapshotDetail"] }) {
  const { repository, stats } = detail;
  const nodes = KIND_LABELS.filter(([kind]) => (stats.nodes_by_kind[kind] ?? 0) > 0);
  const edges = Object.entries(stats.edges_by_kind).filter(([, count]) => count > 0);
  const issues = Object.values(stats.issues_by_severity).reduce((sum, count) => sum + count, 0);
  return (
    <header className="mt-6">
      <p className="font-mono text-sm text-faint">{repository.host}</p>
      <h1 className="mt-1 text-4xl font-light tracking-tight break-words text-ink sm:text-5xl">
        {repository.owner}/<span className="text-signal">{repository.name}</span>
      </h1>
      <p className="mt-3 text-sm text-mist">
        Commit <span className="font-mono break-all text-ink">{detail.commit_sha}</span> · analyzer{" "}
        {detail.extractor_version} · ingested {formatTimestamp(detail.created_at)}
      </p>
      <dl className="mt-6 flex flex-wrap gap-x-8 gap-y-3" aria-label="Snapshot contents">
        {nodes.map(([kind, singular, pluralForm]) => {
          const count = stats.nodes_by_kind[kind] ?? 0;
          return <Reading key={kind} unit={count === 1 ? singular : pluralForm} count={count} />;
        })}
        {edges.map(([kind, count]) => (
          <Reading key={kind} unit={`${kind} edges`} count={count} />
        ))}
        <Reading unit={issues === 1 ? "extraction issue" : "extraction issues"} count={issues} />
      </dl>
    </header>
  );
}

/** One term/value pair: read as "files, 20", shown as "20 files". */
function Reading({ unit, count }: { unit: string; count: number }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="order-2 text-xs text-faint">{unit}</dt>
      <dd className="order-1 font-mono text-lg text-ink">{count.toLocaleString()}</dd>
    </div>
  );
}

/** Where the 3D world will render (S1). It states plainly what this browser can show. */
function Stage() {
  const webgl = useWebGL2();
  const reducedMotion = useReducedMotion();
  return (
    <section
      aria-label="3D world"
      className="relative mt-10 flex min-h-48 items-end overflow-hidden rounded-xl border border-line bg-[radial-gradient(80%_120%_at_50%_120%,rgb(29_79_122/0.55),transparent_70%)] p-6 sm:min-h-64"
    >
      <div className="relative">
        {webgl ? (
          <p className="text-mist">
            <span className="text-ink">The 3D world opens here in the next milestone.</span> Until then, explore it through the
            outline below.
          </p>
        ) : (
          <p className="text-mist">
            <span className="text-ink">This browser cannot show the 3D world</span> (WebGL2 is unavailable). The outline below is the
            complete 2D view of the same world.
          </p>
        )}
        {reducedMotion && <p className="mt-2 text-xs text-faint">Reduced motion is on: camera flights will become short fades.</p>}
      </div>
    </section>
  );
}

function RunSources({
  architecture,
  health,
  snapshotId,
}: {
  architecture: Schemas["DerivationRunSummary"] | null;
  health: Schemas["DerivationRunSummary"] | null;
  snapshotId: number;
}) {
  const missing = [architecture === null ? "derive_architecture" : null, health === null ? "derive_health" : null].filter(
    (name): name is string => name !== null,
  );
  return (
    <>
      <ul className="flex flex-col gap-1 text-sm">
        <RunLine name="Architecture" run={architecture} purpose="islands, districts, bridges and layers" />
        <RunLine name="Health" run={health} purpose="building heights and findings" />
      </ul>
      {missing.length > 0 && (
        <div className="mt-3 max-w-3xl text-sm text-mist">
          <p>Prepare the missing runs with the existing orchestration functions (see frontend/README.md):</p>
          <Command>{`python -c "import os, psycopg; from xraymyrepo.orchestration import ${missing.join(", ")}; conn = psycopg.connect(os.environ['XRAY_DATABASE_URL'], autocommit=True); print(${missing
            .map((name) => `${name}(conn, ${snapshotId})`)
            .join(", ")})"`}</Command>
        </div>
      )}
    </>
  );
}

function RunLine({ name, run, purpose }: { name: string; run: Schemas["DerivationRunSummary"] | null; purpose: string }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-3">
      <span className="w-28 text-faint">{name}</span>
      {run ? (
        <span className="text-ink">
          run <span className="font-mono">#{run.id}</span> · {run.algorithm} {run.algorithm_version} ·{" "}
          {formatTimestamp(run.created_at)}
        </span>
      ) : (
        <span className="text-ember">no complete run — {purpose} are unavailable</span>
      )}
    </li>
  );
}

function SourceNote({ source }: { source: IslandSource }) {
  if (source.kind === "lens") return <LensNote runId={source.runId} lens={source.lens} />;
  return (
    <p className="text-sm text-mist">
      {source.reason === "no_architecture_run"
        ? "No architecture run for this snapshot yet: islands are its top-level directories."
        : "The architecture run has no groups: islands are the snapshot's top-level directories."}
    </p>
  );
}

/**
 * `LensSummary.group_count` counts the lens's groups at every level, so the number of islands
 * comes from the top-level query the outline itself makes (same query key: no extra request).
 */
function LensNote({ runId, lens }: { runId: number; lens: Schemas["LensSummary"] }) {
  const islands = useGroupChildren(runId, lens.name, null);
  const count = islands.data ? islands.data.items.length : null;
  return (
    <p className="text-sm text-mist">
      Islands are the {count === null ? "" : `${islands.data?.truncated ? "first " : ""}${count.toLocaleString()} `}
      top-level {count === 1 ? "group" : "groups"} of the <span className="font-mono text-ink">{lens.name}</span> lens (
      {lens.kind}), which has {plural(lens.group_count, "group")} at every level.
    </p>
  );
}
