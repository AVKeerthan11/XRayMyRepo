import { Link, useSearchParams } from "react-router";

import type { Schemas } from "../api/client";
import { useRepositories, useSnapshots } from "../api/queries";
import { formatTimestamp, parseSlug, plural, shortSha } from "../ui/format";
import { Command, EmptyState, ErrorState, Eyebrow, Loading } from "../ui/Status";

/** The observatory: choose a repository, then one of its snapshots, then enter its world. */
export function Home() {
  const [params] = useSearchParams();
  const selectedSlug = params.get("repository");
  const repositories = useRepositories();

  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="max-w-2xl text-4xl leading-tight font-light tracking-tight text-ink sm:text-5xl">
        Choose a codebase to explore.
      </h1>
      <p className="mt-4 max-w-xl text-mist">
        Every world is built from one immutable snapshot: its real files, groups and dependencies.
      </p>

      <div className="mt-12 grid gap-12 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-16">
        <section aria-labelledby="repositories-heading">
          <Eyebrow id="repositories-heading">Repositories</Eyebrow>
          <div className="mt-4">
            {repositories.isPending ? (
              <Loading label="Scanning the archive for repositories…" />
            ) : repositories.isError ? (
              <ErrorState error={repositories.error} onRetry={() => void repositories.refetch()} />
            ) : repositories.data.items.length === 0 ? (
              <EmptyState title="No repositories have been ingested yet">
                <p>Ingest one with the command line, then reload:</p>
                <Command>{"python -m xraymyrepo ingest https://github.com/OWNER/NAME --ref main"}</Command>
              </EmptyState>
            ) : (
              <RepositoryList
                repositories={repositories.data.items}
                truncated={repositories.data.truncated}
                selectedSlug={selectedSlug}
              />
            )}
          </div>
        </section>

        <section aria-labelledby="snapshots-heading" aria-live="polite">
          <Eyebrow id="snapshots-heading">Snapshots</Eyebrow>
          <div className="mt-4">
            <SnapshotsFor slug={selectedSlug} repositories={repositories.data?.items} />
          </div>
        </section>
      </div>
    </div>
  );
}

function RepositoryList({
  repositories,
  truncated,
  selectedSlug,
}: {
  repositories: Schemas["Repository"][];
  truncated: boolean;
  selectedSlug: string | null;
}) {
  return (
    <>
      <ul className="divide-y divide-line border-y border-line">
        {repositories.map((repository) => {
          const selected = repository.slug === selectedSlug;
          return (
            <li key={repository.slug}>
              <Link
                to={`/?repository=${encodeURIComponent(repository.slug)}`}
                aria-current={selected ? "true" : undefined}
                className={`group flex min-h-14 flex-col justify-center gap-1 py-3 pr-2 pl-4 transition-colors duration-(--duration-quick) ${
                  selected ? "border-l-2 border-signal bg-deep/60" : "border-l-2 border-transparent hover:bg-deep/40"
                }`}
              >
                <span className={`font-mono text-sm break-all ${selected ? "text-signal" : "text-ink group-hover:text-signal"}`}>
                  {repository.slug}
                </span>{" "}
                <span className="text-xs text-faint">
                  {plural(repository.snapshot_count, "snapshot")}
                  {repository.latest_snapshot && ` · latest ${shortSha(repository.latest_snapshot.commit_sha)}`}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {truncated && (
        <p className="mt-3 text-xs text-faint">Showing the first {repositories.length.toLocaleString()} repositories.</p>
      )}
    </>
  );
}

function SnapshotsFor({ slug, repositories }: { slug: string | null; repositories: Schemas["Repository"][] | undefined }) {
  const parsed = parseSlug(slug);
  const known = repositories?.find((r) => r.slug === slug);
  const reference = known ? { host: known.host, owner: known.owner, name: known.name } : null;
  const snapshots = useSnapshots(reference);

  if (slug === null) {
    return <p className="py-2 text-mist">Select a repository to see its snapshots.</p>;
  }
  if (repositories === undefined) return null; // the repository list is still loading or failed
  if (parsed === null || known === undefined) {
    return <EmptyState title="That repository is not in the archive">Choose one from the list.</EmptyState>;
  }
  if (snapshots.isPending) return <Loading label={`Reading snapshots of ${known.slug}…`} />;
  if (snapshots.isError) return <ErrorState error={snapshots.error} onRetry={() => void snapshots.refetch()} />;
  if (snapshots.data.items.length === 0) {
    return (
      <EmptyState title="No complete snapshots yet">
        <p>Ingest a commit of this repository:</p>
        <Command>{`python -m xraymyrepo ingest https://${known.slug} --ref main`}</Command>
      </EmptyState>
    );
  }
  return (
    <>
      <h2 className="sr-only">Snapshots of {known.slug}</h2>
      <ol className="divide-y divide-line border-y border-line">
        {snapshots.data.items.map((snapshot) => (
          <li key={snapshot.id}>
            <Link
              to={`/world/${snapshot.id}`}
              className="group flex min-h-14 flex-wrap items-center justify-between gap-x-6 gap-y-1 py-3 pr-2 pl-4 transition-colors duration-(--duration-quick) hover:bg-deep/40"
            >
              <span className="flex flex-col gap-1">
                <span className="font-mono text-sm text-ink group-hover:text-signal">{shortSha(snapshot.commit_sha)}</span>{" "}
                <span className="text-xs text-faint">
                  Ingested {formatTimestamp(snapshot.created_at)} · analyzer {snapshot.extractor_version}
                </span>
              </span>{" "}
              <span className="text-sm text-mist transition-transform duration-(--duration-quick) group-hover:translate-x-1 group-hover:text-signal">
                Enter world <span aria-hidden="true">→</span>
              </span>
            </Link>
          </li>
        ))}
      </ol>
      {snapshots.data.truncated && (
        <p className="mt-3 text-xs text-faint">Showing the {snapshots.data.items.length} newest snapshots.</p>
      )}
    </>
  );
}
