import { readFileSync } from "node:fs";

import { formatDate } from "../updates";
import { formatDate as cardFormatDate } from "../../pages/package/packageHelpers";

describe("dates", () => {
  it("uses one format on the card and in Updates: the reader's locale, never ISO", () => {
    expect(cardFormatDate).toBe(formatDate);
    const text = formatDate("2026-11-05T09:00:00Z");
    expect(text).toBe(new Date("2026-11-05T09:00:00Z").toLocaleDateString());
    expect(text).not.toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("is empty for no date, not 1970", () => {
    expect(formatDate(null)).toBe("");
    expect(formatDate(undefined)).toBe("");
    expect(formatDate("")).toBe("");
  });
});

describe("the page title", () => {
  it("is PackageHub", () => {
    const html = readFileSync("index.html", "utf8");   // tests run from the frontend folder
    expect(html).toContain("<title>PackageHub</title>");
  });
});
