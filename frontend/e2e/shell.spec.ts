import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import { GOLDEN_SLUG } from "../src/test/fixtures";
import { isApiRequest, mockApi } from "./api";

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

async function seriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

test("choose a repository and snapshot, then explore the world outline", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: new RegExp(GOLDEN_SLUG) }).click();
  await expect(page).toHaveURL(/repository=github\.com%2Fxraymyrepo%2Fgolden-fixture/);
  await page.getByRole("link", { name: /^0123456789 .*Enter world$/ }).click();

  await expect(page).toHaveURL(/\/world\/2$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("xraymyrepo/golden-fixture");
  const outline = page.getByRole("region", { name: "World outline" });
  await outline.getByRole("button", { name: "Expand acme-backend" }).click();
  await outline.getByRole("button", { name: "Expand app.users" }).click();
  await outline.getByRole("button", { name: /^service\.py/ }).click();

  const panel = page.getByRole("complementary", { name: "Inspection" });
  await expect(panel).toContainText("app.users.service");
  await expect(panel).toContainText("2 out · 1 in");

  await page.getByRole("link", { name: "Observatory", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
});

test("the world outline works with the keyboard alone", async ({ page, isMobile }) => {
  test.skip(isMobile, "keyboard navigation is a desktop concern");
  await page.goto("/world/2");
  await expect(page.getByRole("button", { name: "Expand acme-backend" })).toBeVisible();

  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");

  async function tabTo(name: string | RegExp) {
    const target = page.getByRole("button", { name });
    for (let i = 0; i < 40; i++) {
      if (await target.evaluate((el) => el === document.activeElement)) return;
      await page.keyboard.press("Tab");
    }
    throw new Error(`could not reach "${name}" with Tab`);
  }

  await tabTo("Expand acme-backend");
  await page.keyboard.press("Enter");
  // Expanded, the toggle now offers the opposite action.
  await expect(page.getByRole("button", { name: "Collapse acme-backend" })).toHaveAttribute("aria-expanded", "true");
  await tabTo("Expand app.users");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: /^service\.py/ })).toBeVisible();
  await tabTo(/^service\.py\s*, python$/);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", { name: "Inspection" })).toContainText("app.users.service");
});

test("no serious accessibility violations on the observatory and in a world", async ({ page }) => {
  await page.goto(`/?repository=${encodeURIComponent(GOLDEN_SLUG)}`);
  await expect(page.getByRole("link", { name: /^0123456789 .*Enter world$/ })).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);

  await page.goto("/world/2");
  const outline = page.getByRole("region", { name: "World outline" });
  await outline.getByRole("button", { name: "Expand acme-backend" }).click();
  await outline.getByRole("button", { name: /^app\.users/ }).click();
  await expect(page.getByRole("complementary", { name: "Inspection" })).toContainText("instability");
  expect(await seriousViolations(page)).toEqual([]);
});

test("touch targets are at least 44px and nothing scrolls sideways", async ({ page }) => {
  await page.goto("/world/2");
  const outline = page.getByRole("region", { name: "World outline" });
  await outline.getByRole("button", { name: "Expand acme-backend" }).click();
  await expect(outline.getByRole("button", { name: "Expand app.users" })).toBeVisible();

  const small = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("main button, main a")]
      .filter((el) => el.offsetParent !== null && !el.closest("nav[aria-label='Breadcrumb'], p"))
      .map((el) => ({ label: el.getAttribute("aria-label") ?? el.textContent ?? "", height: el.getBoundingClientRect().height }))
      .filter((target) => target.height < 44),
  );
  expect(small).toEqual([]);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("without WebGL2 the world says so and the outline is the complete view", async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, ...rest: unknown[]) {
      if (type === "webgl2") return null;
      return (original as (...args: unknown[]) => unknown).call(this, type, ...rest);
    } as typeof HTMLCanvasElement.prototype.getContext;
  });
  await page.goto("/world/2");
  await expect(page.getByRole("region", { name: "3D world" })).toContainText("cannot show the 3D world");
  await expect(page.getByRole("button", { name: "Expand acme-backend" })).toBeVisible();
});

test("with WebGL2 the stage reserves its place for the 3D world", async ({ page }) => {
  await page.goto("/world/2");
  const webgl = await page.evaluate(() => document.createElement("canvas").getContext("webgl2") !== null);
  test.skip(!webgl, "this Chromium build has no WebGL2");
  await expect(page.getByRole("region", { name: "3D world" })).toContainText("opens here in the next milestone");
});

test("reduced motion is honoured", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/world/2");
  await expect(page.getByRole("region", { name: "3D world" })).toContainText("Reduced motion is on");
  const duration = await page
    .getByRole("button", { name: "Expand acme-backend" })
    .evaluate((el) => getComputedStyle(el.closest("div") as HTMLElement).transitionDuration);
  expect(Number.parseFloat(duration)).toBeLessThanOrEqual(0.0001);
});

test("an unreachable API is explained, not shown as an empty archive", async ({ page }) => {
  await page.unroute(isApiRequest);
  await page.route(isApiRequest, (route) => route.fulfill({ status: 502, contentType: "text/html", body: "<h1>Bad gateway</h1>" }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("lost contact with the API");
  await expect(page.getByText(/no repositories have been ingested/i)).toHaveCount(0);
});
