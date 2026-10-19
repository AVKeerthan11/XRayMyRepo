import type { ReactNode } from "react";

import { ApiError } from "../api/errors";

/** A quiet, announced loading line. */
export function Loading({ label }: { label: string }) {
  return (
    <p role="status" aria-live="polite" className="flex items-center gap-3 py-6 text-sm text-mist">
      <span aria-hidden="true" className="size-1.5 animate-pulse rounded-full bg-signal" />
      {label}
    </p>
  );
}

/** Copy for each failure the API layer distinguishes; never a raw stack or URL. */
export function describeError(error: unknown): { title: string; detail: string; hint?: string } {
  if (error instanceof ApiError) {
    if (error.kind === "unreachable") {
      return {
        title: "The observatory has lost contact with the API",
        detail: "No answer from the XRayMyRepo API.",
        hint: "Start it with: uvicorn --factory xraymyrepo.api:create_app (the dev server proxies /api to 127.0.0.1:8000).",
      };
    }
    if (error.kind === "problem") {
      return { title: error.title, detail: error.message, hint: error.code ? `Code: ${error.code}` : undefined };
    }
    return { title: error.title, detail: error.message };
  }
  return { title: "Something went wrong", detail: "An unexpected error occurred in the page." };
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { title, detail, hint } = describeError(error);
  return (
    <div role="alert" className="border-l-2 border-fault py-2 pl-4">
      <p className="font-medium text-ink">{title}</p>
      <p className="mt-1 text-sm text-mist">{detail}</p>
      {hint && <p className="mt-1 font-mono text-xs break-words text-faint">{hint}</p>}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 min-h-11 rounded-md border border-line-strong px-4 text-sm text-ink transition-colors duration-(--duration-quick) hover:border-signal hover:text-signal"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="border-l-2 border-line-strong py-2 pl-4">
      <p className="font-medium text-ink">{title}</p>
      {children && <div className="mt-1 text-sm text-mist">{children}</div>}
    </div>
  );
}

/** A small uppercase label above a section: the hierarchy's quietest level. */
export function Eyebrow({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <p id={id} className="text-xs font-medium tracking-[0.2em] text-faint uppercase">
      {children}
    </p>
  );
}

export function Command({ children }: { children: string }) {
  return (
    <code className="mt-2 block overflow-x-auto rounded-md bg-abyss px-3 py-2 font-mono text-xs whitespace-pre text-ink">
      {children}
    </code>
  );
}
