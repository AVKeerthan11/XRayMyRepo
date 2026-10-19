import type { Page } from "@playwright/test";

import { respond } from "../src/test/fixtures";

/**
 * Only the API itself. A glob like "**\/api/**" would also catch the dev server's own modules
 * (/src/api/client.ts) and break the app.
 */
export const isApiRequest = (url: URL): boolean => url.pathname.startsWith("/api/");

/** Answer every API request in the browser from the contract-typed fixtures. */
export async function mockApi(page: Page): Promise<void> {
  await page.route(isApiRequest, async (route) => {
    const { status, body } = respond(new URL(route.request().url()));
    await route.fulfill({
      status,
      contentType: status >= 400 ? "application/problem+json" : "application/json",
      body: JSON.stringify(body),
    });
  });
}
