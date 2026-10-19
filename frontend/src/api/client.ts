import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/** Response and value types of the read-only API, generated from docs/api/openapi-v1.json. */
export type Schemas = components["schemas"];

/**
 * A typed client for the XRayMyRepo API. In the browser the API is same-origin: the Vite dev
 * server proxies /api to it (vite.config.ts), so no CORS configuration is needed.
 */
export function createApiClient(baseUrl: string) {
  // Look fetch up on every call: openapi-fetch would otherwise keep the one present at import
  // time, bypassing anything installed later (request mocking in tests, instrumentation).
  return createClient<paths>({ baseUrl, fetch: (request) => globalThis.fetch(request) });
}

export type ApiClient = ReturnType<typeof createApiClient>;

export const api: ApiClient = createApiClient(globalThis.location?.origin ?? "");
