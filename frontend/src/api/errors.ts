import type { Schemas } from "./client";

export type ApiErrorKind =
  /** The API answered with an RFC 9457 problem: a stable `code`, `title` and `detail`. */
  | "problem"
  /** No API answered: the server is down, or the dev proxy could not reach it. */
  | "unreachable"
  /** An answer the contract does not describe. */
  | "unexpected";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly code: string | null;
  readonly title: string;

  constructor(kind: ApiErrorKind, title: string, detail: string, status: number | null, code: string | null) {
    super(detail);
    this.name = "ApiError";
    this.kind = kind;
    this.title = title;
    this.status = status;
    this.code = code;
  }
}

export function isProblem(value: unknown): value is Schemas["Problem"] {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.code === "string" &&
    typeof v.status === "number" &&
    typeof v.title === "string" &&
    typeof v.detail === "string"
  );
}

/** Statuses a proxy or gateway answers with when the API behind it is not running. */
const GATEWAY_STATUSES = new Set([500, 502, 503, 504]);

export function toApiError(response: Response | null, body: unknown): ApiError {
  if (isProblem(body)) {
    return new ApiError("problem", body.title, body.detail, body.status, body.code);
  }
  if (response === null || GATEWAY_STATUSES.has(response.status)) {
    return new ApiError("unreachable", "API unreachable", "The XRayMyRepo API did not answer.", response?.status ?? null, null);
  }
  return new ApiError(
    "unexpected",
    "Unexpected response",
    `The API answered ${response.status} without a problem description.`,
    response.status,
    null,
  );
}

interface ClientResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** The data of a successful call, or an ``ApiError`` describing why there is none. */
export async function unwrap<T>(call: Promise<ClientResult<T>>): Promise<T> {
  let result: ClientResult<T>;
  try {
    result = await call;
  } catch {
    throw toApiError(null, undefined); // fetch itself failed: no server answered
  }
  if (result.response.ok && result.data !== undefined) return result.data;
  throw toApiError(result.response, result.error);
}

/** Only an unreachable API is worth retrying; a problem answer is the API's final word. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  return error instanceof ApiError && error.kind === "unreachable" && failureCount < 1;
}
