import { ApiError } from "./errors";

/** The shape of every paged API response (`Page[T]`). */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Collected<T> {
  items: T[];
  /** True when more items exist than ``maxItems``; the caller must say so, never hide it. */
  truncated: boolean;
}

/** The API's largest page (`limit` maximum in the OpenAPI contract). */
export const MAX_PAGE_SIZE = 1000;

/**
 * Follow `next_cursor` until the list ends or ``maxItems`` are collected. Bounded by design:
 * a repository with 50 000 files must never be pulled into the browser by accident.
 */
export async function collectPages<T>(
  fetchPage: (cursor: string | undefined, limit: number) => Promise<Page<T>>,
  maxItems: number,
): Promise<Collected<T>> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  for (;;) {
    const limit = Math.min(MAX_PAGE_SIZE, maxItems - items.length);
    const page = await fetchPage(cursor, limit);
    items.push(...page.items);
    const next = page.next_cursor;
    if (items.length >= maxItems) {
      return { items: items.slice(0, maxItems), truncated: items.length > maxItems || next !== null };
    }
    if (next === null) return { items, truncated: false };
    if (seen.has(next)) {
      throw new ApiError("unexpected", "Unexpected response", "The API repeated a page cursor.", null, null);
    }
    seen.add(next);
    cursor = next;
  }
}
