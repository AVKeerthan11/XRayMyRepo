import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { PENDING_SNAPSHOT_ID } from "../test/fixtures";
import { renderRoute } from "../test/render";
import { server } from "../test/server";
import { parseSnapshotId } from "./World";

async function outline() {
  const section = await screen.findByRole("region", { name: "World outline" });
  return within(section);
}

describe("parseSnapshotId", () => {
  it("accepts only positive integers", () => {
    expect(parseSnapshotId("42")).toBe(42);
    for (const bad of [undefined, "", "0", "-1", "1.5", "abc", "1e3", "01", "99999999999999999999"]) {
      expect(parseSnapshotId(bad)).toBeNull();
    }
  });
});

describe("World", () => {
  it("shows the snapshot, its runs, and islands from the preferred lens", async () => {
    renderRoute("/world/2");
    expect(screen.getByRole("status")).toHaveTextContent(/charting snapshot 2/i);

    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("xraymyrepo/golden-fixture");
    expect(screen.getByText("0123456789abcdef0123456789abcdef01234567")).toBeInTheDocument();
    // Term/value pairs straight from SnapshotDetail.stats.
    const readout = within(screen.getByLabelText("Snapshot contents"));
    const valueOf = (term: string) => readout.getByText(term, { selector: "dt" }).nextElementSibling;
    expect(valueOf("files")).toHaveTextContent("20");
    expect(valueOf("directories")).toHaveTextContent("9");
    expect(valueOf("external packages")).toHaveTextContent("4");
    expect(valueOf("IMPORTS edges")).toHaveTextContent("14");
    expect(valueOf("extraction issues")).toHaveTextContent("3");

    // Run 12 is pending and 9 failed: the newest complete runs are used.
    const sources = screen.getByRole("region", { name: "World sources" });
    await within(sources).findByRole("list");
    expect(sources).toHaveTextContent(/Architecture\s*run #10 · architecture 1\.0\.0/);
    expect(sources).toHaveTextContent(/Health\s*run #11 · health 1\.0\.0/);
    expect(sources).not.toHaveTextContent(/#12|#9/);

    const world = await outline();
    // The island count is the top-level groups, not the lens's group_count (every level).
    expect(await world.findByText(/islands are the 3 top-level groups of the/i)).toHaveTextContent(
      "components lens (inferred), which has 5 groups at every level.",
    );
    // Island names carry their real size: metrics.files and child_group_count.
    expect(await world.findByRole("button", { name: "acme-backend, 9 files · 2 districts" })).toBeInTheDocument();
    expect(world.getByRole("button", { name: "manage, 1 file" })).toBeInTheDocument();
    expect(world.getByRole("button", { name: /^tests/ })).toBeInTheDocument();
    expect(world.getByRole("button", { name: /^manage/ })).toBeInTheDocument();
  });

  it("expands an island into districts and files, and inspects a file", async () => {
    const user = userEvent.setup();
    renderRoute("/world/2");
    const world = await outline();

    const expand = await world.findByRole("button", { name: "Expand acme-backend" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    expect(expand).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById(expand.getAttribute("aria-controls") ?? "")).not.toBeNull();

    await user.click(await world.findByRole("button", { name: "Expand app.users" }));
    const service = await world.findByRole("button", { name: /service\.py/ });
    await user.click(service);
    expect(service).toHaveAttribute("aria-current", "true");

    const panel = within(screen.getByRole("complementary", { name: "Inspection" }));
    expect(await panel.findByText("app.users.service")).toBeInTheDocument();
    expect(panel.getByText("2 out · 1 in")).toBeInTheDocument();
    expect(panel.getByText("backend/app/users/service.py")).toBeInTheDocument();
  });

  it("inspects a group with its real metrics and layer mix", async () => {
    const user = userEvent.setup();
    renderRoute("/world/2");
    const world = await outline();
    await user.click(await world.findByRole("button", { name: "Expand acme-backend" }));
    await user.click(await world.findByRole("button", { name: /^app\.users/ }));
    const panel = within(screen.getByRole("complementary", { name: "Inspection" }));
    expect(panel.getByText("instability")).toBeInTheDocument();
    expect(panel.getByText("0.67")).toBeInTheDocument();
    expect(panel.getByText("application 1 · interface 2 · unclassified 0")).toBeInTheDocument();
    expect(panel.getByText("medium (inferred)")).toBeInTheDocument();
  });

  it("falls back to top-level directories without an architecture run, and says how to prepare one", async () => {
    const user = userEvent.setup();
    renderRoute("/world/3");
    const world = await outline();
    expect(await world.findByText(/no architecture run for this snapshot yet/i)).toBeInTheDocument();
    expect(screen.getAllByText(/no complete run/)).toHaveLength(2);
    expect(screen.getByText(/from xraymyrepo\.orchestration import derive_architecture, derive_health/)).toHaveTextContent(
      "derive_architecture(conn, 3), derive_health(conn, 3)",
    );

    expect(world.queryByRole("button", { name: "Expand README.md" })).toBeNull(); // files are leaves
    await user.click(await world.findByRole("button", { name: "Expand backend" }));
    expect(await world.findByRole("button", { name: /pyproject\.toml/ })).toBeInTheDocument();
  });

  it("tells the user this browser cannot show the 3D world (jsdom has no WebGL)", async () => {
    renderRoute("/world/2");
    const stage = await screen.findByRole("region", { name: "3D world" });
    expect(stage).toHaveTextContent(/cannot show the 3D world/i);
    expect(stage).toHaveTextContent(/outline below is the complete 2D view/i);
  });

  it.each([
    ["/world/99", "Snapshot 99 does not exist."],
    [`/world/${PENDING_SNAPSHOT_ID}`, `Snapshot ${PENDING_SNAPSHOT_ID} is pending.`],
  ])("shows the API's problem for %s", async (path, detail) => {
    renderRoute(path);
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("link", { name: "Back to the observatory" })).toHaveAttribute("href", "/");
  });

  it("rejects an address that is not a snapshot id without calling the API", async () => {
    renderRoute("/world/not-a-number");
    expect(screen.getByText(/that is not a snapshot/i)).toBeInTheDocument();
  });

  it("keeps the snapshot visible when the derivation list fails", async () => {
    server.use(http.get("*/api/v1/snapshots/2/derivations", () => HttpResponse.error()));
    renderRoute("/world/2");
    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    const sources = within(screen.getByRole("region", { name: "World sources" }));
    expect(await sources.findByRole("alert")).toHaveTextContent(/lost contact with the api/i);
  });

  it("shows an unknown route as off the map", () => {
    renderRoute("/nowhere");
    expect(screen.getByRole("heading", { name: /not on the map/i })).toBeInTheDocument();
  });
});
