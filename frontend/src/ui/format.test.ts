import { describe, expect, it } from "vitest";

import { formatTimestamp, parseSlug, plural, shortSha } from "./format";

describe("format", () => {
  it("shortens SHAs to 10 characters", () => {
    expect(shortSha("0123456789abcdef0123456789abcdef01234567")).toBe("0123456789");
  });

  it("pluralizes with an optional irregular form", () => {
    expect(plural(1, "file")).toBe("1 file");
    expect(plural(2, "file")).toBe("2 files");
    expect(plural(0, "entry", "entries")).toBe("0 entries");
  });

  it("leaves an unparseable timestamp as it is", () => {
    expect(formatTimestamp("not a date")).toBe("not a date");
    expect(formatTimestamp("2026-10-12T09:30:00Z")).not.toBe("2026-10-12T09:30:00Z");
  });

  it("parses only HOST/OWNER/NAME slugs", () => {
    expect(parseSlug("github.com/acme/app")).toEqual({ host: "github.com", owner: "acme", name: "app" });
    for (const bad of [null, "", "github.com/acme", "github.com/acme/app/extra", "github.com//app"]) {
      expect(parseSlug(bad)).toBeNull();
    }
  });
});
