import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

// Any request the fixtures do not describe fails the test (MSW 3: onUnhandledFrame).
beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

// jsdom has no canvas at all; give it the answer of a browser without WebGL. A plain stub, not a
// spy, so `restoreMocks` does not bring back jsdom's "not implemented" error between tests.
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
