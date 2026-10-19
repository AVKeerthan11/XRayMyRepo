import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./errors";
import { collectPages, MAX_PAGE_SIZE, type Page } from "./paginate";

function pages(...chunks: number[][]): (cursor: string | undefined, limit: number) => Promise<Page<number>> {
  return (cursor) => {
    const index = cursor === undefined ? 0 : Number(cursor);
    const items = chunks[index] ?? [];
    return Promise.resolve({ items, next_cursor: index + 1 < chunks.length ? String(index + 1) : null });
  };
}

describe("collectPages", () => {
  it("follows cursors to the end", async () => {
    await expect(collectPages(pages([1, 2], [3], [4, 5]), 100)).resolves.toEqual({ items: [1, 2, 3, 4, 5], truncated: false });
  });

  it("stops at the limit and says the list is truncated", async () => {
    await expect(collectPages(pages([1, 2], [3, 4], [5]), 3)).resolves.toEqual({ items: [1, 2, 3], truncated: true });
  });

  it("is not truncated when the limit is reached exactly at the end", async () => {
    await expect(collectPages(pages([1, 2], [3]), 3)).resolves.toEqual({ items: [1, 2, 3], truncated: false });
  });

  it("asks for at most the API's maximum page size, and no more than still needed", async () => {
    const fetchPage = vi.fn(pages([1, 2], [3]));
    await collectPages(fetchPage, 2500);
    expect(fetchPage).toHaveBeenNthCalledWith(1, undefined, MAX_PAGE_SIZE);
    await collectPages(fetchPage, 5);
    expect(fetchPage).toHaveBeenLastCalledWith("1", 3);
  });

  it("refuses a repeated cursor instead of looping forever", async () => {
    const loop = () => Promise.resolve({ items: [1], next_cursor: "same" });
    await expect(collectPages(loop, 100)).rejects.toBeInstanceOf(ApiError);
  });
});
