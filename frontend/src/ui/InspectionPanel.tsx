import type { ReactNode } from "react";

import type { Schemas } from "../api/client";
import { useNodeDetail } from "../api/queries";
import type { Selection } from "../data/world";
import { plural, shortSha } from "./format";
import { ErrorState, Eyebrow, Loading } from "./Status";

/**
 * Technical facts about the selection, separate from the world itself. Every value is a field
 * of the API response; nothing is computed or scored here.
 */
export function InspectionPanel({ snapshotId, selection }: { snapshotId: number; selection: Selection | null }) {
  return (
    <aside aria-labelledby="inspection-heading" className="lg:sticky lg:top-8">
      <Eyebrow id="inspection-heading">Inspection</Eyebrow>
      <div aria-live="polite" className="mt-4">
        {selection === null ? (
          <p className="text-sm text-mist">Select an island, district or file to inspect it.</p>
        ) : selection.type === "group" ? (
          <GroupFacts group={selection.group} />
        ) : (
          <NodeFacts snapshotId={snapshotId} node={selection.node} />
        )}
      </div>
    </aside>
  );
}

function GroupFacts({ group }: { group: Schemas["GroupSummary"] }) {
  const metrics = Object.entries(group.metrics).sort(([a], [b]) => a.localeCompare(b));
  const layers = Object.entries(group.layer_mix.by_layer).sort(([a], [b]) => a.localeCompare(b));
  return (
    <>
      <Title name={group.label ?? group.key} sub={group.key} />
      <Facts>
        <Fact term="Lens">{group.lens}</Fact>
        <Fact term="Confidence">
          {group.confidence} ({group.basis})
        </Fact>
        <Fact term="Direct files">{group.member_count.toLocaleString()}</Fact>
        <Fact term="Districts">{group.child_group_count.toLocaleString()}</Fact>
        {metrics.map(([name, value]) => (
          <Fact key={name} term={name} mono>
            {Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2)}
          </Fact>
        ))}
        {(layers.length > 0 || group.layer_mix.unclassified > 0) && (
          <Fact term="Layers">
            {[...layers.map(([layer, count]) => `${layer} ${count}`), `unclassified ${group.layer_mix.unclassified}`].join(" · ")}
          </Fact>
        )}
      </Facts>
    </>
  );
}

function NodeFacts({ snapshotId, node }: { snapshotId: number; node: Schemas["NodeSummary"] }) {
  const detail = useNodeDetail(snapshotId, node.key);
  return (
    <>
      <Title name={node.name} sub={node.key} />
      {detail.isPending ? (
        <Loading label="Reading the file record…" />
      ) : detail.isError ? (
        <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
      ) : (
        <NodeDetailFacts detail={detail.data} />
      )}
    </>
  );
}

function NodeDetailFacts({ detail }: { detail: Schemas["NodeDetail"] }) {
  const moduleName = "module_name" in detail.attributes ? detail.attributes.module_name : null;
  const relationships = Object.entries(detail.relationship_counts).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Facts>
      <Fact term="Kind">{detail.kind}</Fact>
      {detail.language && <Fact term="Language">{detail.language}</Fact>}
      {detail.extraction_status && <Fact term="Extraction">{detail.extraction_status}</Fact>}
      {moduleName && (
        <Fact term="Module" mono>
          {moduleName}
        </Fact>
      )}
      {detail.child_count > 0 && <Fact term="Contains">{plural(detail.child_count, "declaration")}</Fact>}
      {relationships.map(([kind, count]) => (
        <Fact key={kind} term={kind} mono>
          {count.outgoing.toLocaleString()} out · {count.incoming.toLocaleString()} in
        </Fact>
      ))}
      <Fact term="Diagnostics">
        {plural(detail.diagnostic_counts.extraction_issues, "issue")} ·{" "}
        {plural(detail.diagnostic_counts.unresolved_references, "unresolved reference")}
      </Fact>
      {detail.tags.length > 0 && <Fact term="Tags">{detail.tags.map((tag) => `${tag.facet}: ${tag.value}`).join(" · ")}</Fact>}
      {detail.blob_sha && (
        <Fact term="Blob" mono>
          {shortSha(detail.blob_sha)}
        </Fact>
      )}
    </Facts>
  );
}

function Title({ name, sub }: { name: string; sub: string }) {
  return (
    <div>
      <p className="text-2xl font-light tracking-tight break-words text-ink">{name}</p>
      <p className="mt-1 font-mono text-xs break-all text-faint">{sub}</p>
    </div>
  );
}

function Facts({ children }: { children: ReactNode }) {
  return <dl className="mt-6 divide-y divide-line border-y border-line">{children}</dl>;
}

function Fact({ term, children, mono }: { term: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-6 py-2.5">
      <dt className="text-xs tracking-wide text-faint">{term}</dt>
      <dd className={`text-right text-sm break-words text-ink ${mono ? "font-mono" : ""}`}>{children}</dd>
    </div>
  );
}
