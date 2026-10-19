import { describe, expect, it } from "vitest";

import { ApiError, isProblem, shouldRetry, toApiError, unwrap } from "./errors";

const problem = {
  type: "urn:xraymyrepo:problem:snapshot_not_found",
  title: "Not found",
  status: 404,
  detail: "Snapshot 9 does not exist.",
  code: "snapshot_not_found",
};

function result<T>(status: number, data?: T, error?: unknown) {
  return Promise.resolve({ data, error, response: new Response(null, { status }) });
}

describe("unwrap", () => {
  it("returns the data of a successful call", async () => {
    await expect(unwrap(result(200, { items: [] }))).resolves.toEqual({ items: [] });
  });

  it("turns an RFC 9457 problem into a problem error with its code", async () => {
    const error = await unwrap(result(404, undefined, problem)).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ kind: "problem", status: 404, code: "snapshot_not_found", title: "Not found" });
    expect((error as ApiError).message).toBe("Snapshot 9 does not exist.");
  });

  it("keeps the API's own 503 problem (database unavailable) a problem", async () => {
    const error = await unwrap(
      result(503, undefined, { ...problem, status: 503, code: "database_unavailable", title: "Service unavailable" }),
    ).catch((e: unknown) => e);
    expect(error).toMatchObject({ kind: "problem", code: "database_unavailable" });
  });

  it("reports a failed fetch as an unreachable API", async () => {
    const error = await unwrap(Promise.reject(new TypeError("fetch failed"))).catch((e: unknown) => e);
    expect(error).toMatchObject({ kind: "unreachable", status: null });
  });

  it("reports a proxy error page (no problem body) as an unreachable API", async () => {
    const error = await unwrap(result(502, undefined, "<html>Bad gateway</html>")).catch((e: unknown) => e);
    expect(error).toMatchObject({ kind: "unreachable", status: 502 });
  });

  it("reports an undocumented answer as unexpected", async () => {
    const error = await unwrap(result(418, undefined, "teapot")).catch((e: unknown) => e);
    expect(error).toMatchObject({ kind: "unexpected", status: 418 });
  });
});

describe("helpers", () => {
  it("recognizes problems only with every required field", () => {
    expect(isProblem(problem)).toBe(true);
    expect(isProblem({ ...problem, code: undefined })).toBe(false);
    expect(isProblem("not found")).toBe(false);
    expect(isProblem(null)).toBe(false);
  });

  it("retries only an unreachable API, once", () => {
    expect(shouldRetry(0, toApiError(null, undefined))).toBe(true);
    expect(shouldRetry(1, toApiError(null, undefined))).toBe(false);
    expect(shouldRetry(0, toApiError(new Response(null, { status: 404 }), problem))).toBe(false);
    expect(shouldRetry(0, new Error("render"))).toBe(false);
  });
});
