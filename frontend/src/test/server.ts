import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

import { respond } from "./fixtures";

/** Serves every /api request from the shared fixtures; tests override routes with server.use. */
export const handlers = [
  http.get("*/api/*", ({ request }) => {
    const { status, body } = respond(new URL(request.url));
    const contentType = status >= 400 ? "application/problem+json" : "application/json";
    return HttpResponse.json(body as Record<string, unknown>, { status, headers: { "Content-Type": contentType } });
  }),
];

export const server = setupServer(...handlers);
