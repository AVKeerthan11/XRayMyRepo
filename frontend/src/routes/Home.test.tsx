import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { EMPTY_SLUG, GOLDEN_SLUG } from "../test/fixtures";
import { renderRoute } from "../test/render";
import { server } from "../test/server";

describe("Home", () => {
  it("lists repositories from the API and their snapshots once one is chosen", async () => {
    const user = userEvent.setup();
    renderRoute("/");
    expect(screen.getByRole("status")).toHaveTextContent(/scanning the archive/i);

    const golden = await screen.findByRole("link", { name: new RegExp(GOLDEN_SLUG) });
    expect(golden).toHaveTextContent("2 snapshots · latest 0123456789");
    expect(screen.getByText(/select a repository/i)).toBeInTheDocument();

    await user.click(golden);
    expect(golden).toHaveAttribute("aria-current", "true");
    const enter = await screen.findByRole("link", { name: /^0123456789 .*enter world$/i });
    expect(enter).toHaveAttribute("href", "/world/2");
    expect(screen.getByRole("link", { name: /^89abcdef01 .*enter world$/i })).toHaveAttribute("href", "/world/3");
  });

  it("restores the chosen repository from the URL", async () => {
    renderRoute(`/?repository=${encodeURIComponent(GOLDEN_SLUG)}`);
    expect(await screen.findByRole("link", { name: /^0123456789 .*enter world$/i })).toBeInTheDocument();
  });

  it("explains a repository without complete snapshots, with the ingest command", async () => {
    renderRoute(`/?repository=${encodeURIComponent(EMPTY_SLUG)}`);
    expect(await screen.findByText(/no complete snapshots yet/i)).toBeInTheDocument();
    expect(screen.getByText(`python -m xraymyrepo ingest https://${EMPTY_SLUG} --ref main`)).toBeInTheDocument();
  });

  it("says when the repository in the URL is not in the archive", async () => {
    renderRoute("/?repository=github.com/nobody/nothing");
    expect(await screen.findByText(/not in the archive/i)).toBeInTheDocument();
  });

  it("shows the empty archive with the ingest command", async () => {
    server.use(http.get("*/api/v1/repositories", () => HttpResponse.json({ items: [], next_cursor: null })));
    renderRoute("/");
    expect(await screen.findByText(/no repositories have been ingested yet/i)).toBeInTheDocument();
    expect(screen.getByText(/python -m xraymyrepo ingest https:\/\/github.com\/OWNER\/NAME --ref main/)).toBeInTheDocument();
  });

  it("reports an unreachable API and recovers on retry", async () => {
    const user = userEvent.setup();
    server.use(http.get("*/api/v1/repositories", () => HttpResponse.error(), { once: true }));
    renderRoute("/");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/lost contact with the api/i);
    expect(alert).toHaveTextContent(/uvicorn --factory xraymyrepo\.api:create_app/);
    await user.click(within(alert).getByRole("button", { name: /try again/i }));
    expect(await screen.findByRole("link", { name: new RegExp(GOLDEN_SLUG) })).toBeInTheDocument();
  });

  it("shows the API's own problem when the database is down", async () => {
    server.use(
      http.get("*/api/v1/repositories", () =>
        HttpResponse.json(
          { type: "urn:xraymyrepo:problem:database_unavailable", title: "Service unavailable", status: 503, detail: "The database is unavailable. Try again later.", code: "database_unavailable" },
          { status: 503, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );
    renderRoute("/");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Service unavailable");
    expect(alert).toHaveTextContent("The database is unavailable. Try again later.");
    expect(alert).toHaveTextContent("Code: database_unavailable");
  });

  it("has a skip link to the main content", () => {
    renderRoute("/");
    expect(screen.getByRole("link", { name: /skip to content/i })).toHaveAttribute("href", "#content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "content");
  });
});
