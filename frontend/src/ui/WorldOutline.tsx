import { useId, useState, type ReactNode } from "react";

import type { Schemas } from "../api/client";
import type { Collected } from "../api/paginate";
import { LIMITS, useGroupChildren, useGroupMembers, useTreeChildren } from "../api/queries";
import type { IslandSource, Selection } from "../data/world";
import { plural } from "./format";
import { ErrorState, Loading } from "./Status";

interface OutlineProps {
  snapshotId: number;
  source: IslandSource;
  selectedKey: string | null;
  onSelect: (selection: Selection) => void;
}

/**
 * The world as an accessible outline: islands, districts and files as nested lists. It is the
 * 2D fallback of the 3D world and holds the same data; each level is fetched when opened.
 */
export function WorldOutline(props: OutlineProps) {
  const { source, snapshotId } = props;
  return source.kind === "lens" ? (
    <GroupLevel {...props} runId={source.runId} lens={source.lens.name} parent={null} depth={0} />
  ) : (
    <TreeLevel {...props} snapshotId={snapshotId} parent="/" depth={0} />
  );
}

interface LevelProps extends OutlineProps {
  depth: number;
}

function GroupLevel(props: LevelProps & { runId: number; lens: string; parent: string | null }) {
  const { runId, lens, parent } = props;
  const children = useGroupChildren(runId, lens, parent);
  return (
    <LevelState query={children} what={parent === null ? "islands" : "districts"} empty="No groups here.">
      {(groups) => (
        <ul className="flex flex-col">
          {groups.items.map((group) => (
            <GroupRow key={group.key} {...props} group={group} />
          ))}
          {groups.truncated && <TruncatedNote />}
        </ul>
      )}
    </LevelState>
  );
}

function GroupRow(props: LevelProps & { runId: number; lens: string; group: Schemas["GroupSummary"] }) {
  const { group, runId, depth, selectedKey, onSelect } = props;
  const [expanded, setExpanded] = useState(false);
  const files = group.metrics.files;
  const meta = [
    files !== undefined ? plural(files, "file") : plural(group.member_count, "direct file"),
    group.child_group_count > 0 ? plural(group.child_group_count, "district") : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <OutlineRow
      label={group.label ?? group.key}
      meta={meta}
      depth={depth}
      expandable={group.child_group_count > 0 || group.member_count > 0}
      expanded={expanded}
      onToggle={() => setExpanded((open) => !open)}
      selected={selectedKey === group.key}
      onSelect={() => onSelect({ type: "group", group })}
    >
      {group.child_group_count > 0 && <GroupLevel {...props} parent={group.key} depth={depth + 1} />}
      {group.member_count > 0 && <MemberLevel {...props} runId={runId} group={group.key} depth={depth + 1} />}
    </OutlineRow>
  );
}

function MemberLevel(props: LevelProps & { runId: number; group: string }) {
  const members = useGroupMembers(props.runId, props.group);
  return (
    <LevelState query={members} what="files" empty="No files assigned directly.">
      {(nodes) => <NodeList {...props} nodes={nodes} />}
    </LevelState>
  );
}

function TreeLevel(props: LevelProps & { parent: string }) {
  const children = useTreeChildren(props.snapshotId, props.parent);
  return (
    <LevelState query={children} what={props.parent === "/" ? "islands" : "entries"} empty="This directory is empty.">
      {(nodes) => <NodeList {...props} nodes={nodes} tree />}
    </LevelState>
  );
}

function NodeList(props: LevelProps & { nodes: Collected<Schemas["NodeSummary"]>; tree?: boolean }) {
  const { nodes, tree, depth, selectedKey, onSelect } = props;
  return (
    <ul className="flex flex-col">
      {nodes.items.map((node) => (
        <NodeRow key={node.key} {...props} node={node} expandable={Boolean(tree) && node.kind === "directory" && node.child_count > 0} depth={depth} selected={selectedKey === node.key} onPick={() => onSelect({ type: "node", node })} />
      ))}
      {nodes.truncated && <TruncatedNote />}
    </ul>
  );
}

function NodeRow(
  props: LevelProps & { node: Schemas["NodeSummary"]; expandable: boolean; selected: boolean; onPick: () => void },
) {
  const { node, expandable, depth, selected, onPick } = props;
  const [expanded, setExpanded] = useState(false);
  const meta =
    node.kind === "directory"
      ? plural(node.child_count, "entry", "entries")
      : [node.language, node.extraction_status && node.extraction_status !== "full" ? node.extraction_status : null]
          .filter(Boolean)
          .join(" · ");
  return (
    <OutlineRow
      label={node.name}
      mono
      meta={meta}
      depth={depth}
      expandable={expandable}
      expanded={expanded}
      onToggle={() => setExpanded((open) => !open)}
      selected={selected}
      onSelect={onPick}
    >
      {expandable && <TreeLevel {...props} parent={node.key} depth={depth + 1} />}
    </OutlineRow>
  );
}

function OutlineRow({
  label,
  meta,
  depth,
  mono,
  expandable,
  expanded,
  onToggle,
  selected,
  onSelect,
  children,
}: {
  label: string;
  meta: string;
  depth: number;
  mono?: boolean;
  expandable: boolean;
  expanded: boolean;
  onToggle: () => void;
  selected: boolean;
  onSelect: () => void;
  children?: ReactNode;
}) {
  const contentId = useId();
  return (
    <li>
      <div
        className={`flex min-h-11 items-center gap-1 border-l-2 transition-colors duration-(--duration-quick) ${
          selected ? "border-signal bg-deep/70" : "border-transparent hover:bg-deep/40"
        }`}
        style={{ paddingLeft: `${Math.min(depth, 6) * 1.25}rem` }}
      >
        {expandable ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={contentId}
            aria-label={`${expanded ? "Collapse" : "Expand"} ${label}`}
            onClick={onToggle}
            className="flex size-11 shrink-0 items-center justify-center rounded-md text-mist hover:text-signal"
          >
            <span aria-hidden="true" className={`inline-block transition-transform duration-(--duration-quick) ${expanded ? "rotate-90" : ""}`}>
              ›
            </span>
          </button>
        ) : (
          <span aria-hidden="true" className="size-11 shrink-0" />
        )}
        <button
          type="button"
          aria-current={selected ? "true" : undefined}
          onClick={onSelect}
          className="flex min-h-11 min-w-0 flex-1 items-baseline justify-between gap-4 rounded-md pr-3 text-left"
        >
          <span className={`truncate ${mono ? "font-mono text-sm" : ""} ${selected ? "text-signal" : "text-ink"}`}>{label}</span>
          {meta && (
            <span className="shrink-0 text-xs text-faint">
              <span className="sr-only">,</span> {meta}
            </span>
          )}
        </button>
      </div>
      {expandable && expanded && <div id={contentId}>{children}</div>}
    </li>
  );
}

function LevelState<T>({
  query,
  what,
  empty,
  children,
}: {
  query: { isPending: boolean; isError: boolean; error: unknown; data: Collected<T> | undefined; refetch: () => unknown };
  what: string;
  empty: string;
  children: (data: Collected<T>) => ReactNode;
}) {
  if (query.isPending) return <Loading label={`Charting ${what}…`} />;
  if (query.isError || query.data === undefined) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.data.items.length === 0) return <p className="py-3 pl-12 text-sm text-faint">{empty}</p>;
  return <>{children(query.data)}</>;
}

function TruncatedNote() {
  return (
    <li className="py-2 pl-12 text-xs text-faint">
      Only the first {LIMITS.outlineLevel.toLocaleString()} are listed here; search arrives with the 3D world.
    </li>
  );
}
