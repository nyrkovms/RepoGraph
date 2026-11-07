import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import App from "../../App";
import HomePage from "../HomePage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchCatalogSummary: vi.fn(),
  fetchUpdatesSummary: vi.fn(),
  fetchPackages: vi.fn(),
  fetchPackage: vi.fn(),
  searchPackages: vi.fn(),
}));

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const item = (name, extra = {}) => ({
  ecosystem: "go",
  namespace: "",
  name,
  normalized_name: name,
  purl: `pkg:golang/${name}`,
  origin: "dependency",
  latest_version: "v1.0.0",
  used_by: 0,
  ...extra,
});

const CATALOG = {
  ecosystems: [
    { ecosystem: "rpm", packages: 218 },
    { ecosystem: "go", packages: 214 },
    { ecosystem: "crates", packages: 127 },
  ],
  licenses: [{ license: "MIT", count: 133 }],
  most_used: [item("golang.org/x/sys", { used_by: 50 }), item("github.com/stretchr/testify", { used_by: 45 })],
  latest_releases: [item("github.com/gin-gonic/gin", { latest_version: "v1.10.0", released_at: "2026-11-06T10:00:00Z" })],
  deprecated_in_use: [item("github.com/golang/protobuf", { used_by: 5 })],
  stale_in_use: { packages: 39, years: 3, min_used_by: 5, released_before: "2023-11-07T00:00:00Z" },
};

const finished = (hoursAgo) => new Date(Date.now() - hoursAgo * 3_600_000).toISOString();
const run = (ecosystem, extra = {}) => ({
  ecosystem,
  status: "success",
  started_at: finished(3.2),
  finished_at: finished(3),
  packages_failed: 0,
  ...extra,
});

const WEEK = {
  last_runs: ["pypi", "npm", "go", "crates", "nuget", "maven", "rpm"].map((name) => run(name)),
  totals: { attention: 2, packages: 41, releases: 77, prereleases: 3 },
  packages: [
    {
      ecosystem: "npm",
      name: "left-pad",
      purl: "pkg:npm/left-pad",
      from_version: "1.2.0",
      to_version: "2.0.0",
      max_bump: "major",
      flags: ["yanked"],
      used_by: 9,
      origin: "manual",
    },
    {
      ecosystem: "pypi",
      name: "flask",
      purl: "pkg:pypi/flask",
      from_version: "3.0.0",
      to_version: "3.1.0",
      max_bump: "minor",
      flags: [],
      used_by: 3,
      origin: "seed",
    },
  ],
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname + location.search}</output>;
}

function renderHome() {
  return render(
    <MemoryRouter initialEntries={["/"]} future={FUTURE}>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("HomePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchCatalogSummary.mockResolvedValue(CATALOG);
    api.fetchUpdatesSummary.mockResolvedValue(WEEK);
    api.fetchPackages.mockResolvedValue({ data: [], cursor: null, has_more: false });
    api.searchPackages.mockResolvedValue({ data: [], cursor: null, has_more: false });
  });

  it("shows the search box, ecosystems, status, this week and the three lists", async () => {
    renderHome();

    const input = screen.getByLabelText("Name or purl");
    expect(input).toHaveAttribute("placeholder", "requests, pkg:npm/express");

    const chips = await screen.findByRole("list", { name: "Ecosystems" });
    const links = within(chips).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["All 559", "RPM 218", "Go 214", "crates.io 127"]);
    expect(links[0]).toHaveAttribute("href", "/search");
    expect(links[1]).toHaveAttribute("href", "/search?ecosystem=rpm");

    expect(await screen.findByText(/7 of 7 sources updated · last run/)).toBeInTheDocument();
    expect(screen.getByText("Security advisories: PyPI only")).toBeInTheDocument();
    expect(api.fetchUpdatesSummary).toHaveBeenCalledWith(expect.objectContaining({ period: "week" }));

    expect(screen.getByText(/2 need attention/)).toBeInTheDocument();
    expect(screen.getByText(/41 packages updated/)).toBeInTheDocument();
    expect(screen.getByText(/77\s+releases/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All updates this week" })).toHaveAttribute("href", "/updates?period=week");
    // Only the package with a flag is a row; its usage line is the Updates page's.
    const attention = screen.getByRole("link", { name: "left-pad" });
    expect(attention).toHaveAttribute("href", "/package/npm/left-pad");
    expect(screen.queryByRole("link", { name: "flask" })).not.toBeInTheDocument();
    expect(screen.getByText("yanked")).toBeInTheDocument();
    expect(screen.getByText("major")).toBeInTheDocument();
    expect(screen.getByText("used by 9 packages · tracked directly")).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Most used in catalog" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "golang.org/x/sys" })).toHaveAttribute("href", "/package/go/golang.org/x/sys");
    expect(screen.getByText("50")).toBeInTheDocument();
    expect(screen.getByText("Go counts test dependencies too: go.mod does not separate them.")).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Latest releases" })).toBeInTheDocument();
    expect(screen.getByText(/v1\.10\.0 ·/)).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Deprecated, still used" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "github.com/golang/protobuf" })).toBeInTheDocument();
    expect(screen.getByText("used by 5")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No release in 3+ years, used by 5+" })).toBeInTheDocument();
    const stale = screen.getByRole("link", { name: "39 packages" });
    const target = new URL(stale.getAttribute("href"), "http://x");
    expect(target.pathname).toBe("/search");
    expect(Object.fromEntries(target.searchParams)).toEqual({
      released_before: "2023-11-07T00:00:00Z",
      min_used_by: "5",
      sort: "used_by",
    });
  });

  it("lists up to five deprecated packages and links to the rest only when there are more", async () => {
    const many = Array.from({ length: 5 }, (_, index) => item(`github.com/dep/p${index}`, { used_by: 9 - index }));
    api.fetchCatalogSummary.mockResolvedValue({ ...CATALOG, deprecated_in_use: many, deprecated_in_use_total: 12 });
    renderHome();
    const heading = await screen.findByRole("heading", { name: "Deprecated, still used" });
    const list = heading.nextElementSibling;
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    const more = screen.getByRole("link", { name: "All 12 deprecated packages in use" });
    expect(more).toHaveAttribute("href", "/search?deprecated=true&min_used_by=1&sort=used_by");
  });

  it("has no link to the rest when every deprecated package is listed", async () => {
    api.fetchCatalogSummary.mockResolvedValue({ ...CATALOG, deprecated_in_use_total: 1 });
    renderHome();
    await screen.findByRole("heading", { name: "Deprecated, still used" });
    expect(screen.queryByRole("link", { name: /deprecated packages in use/ })).not.toBeInTheDocument();
  });

  it("explains the used by numbers once under each list that shows them", async () => {
    renderHome();
    await screen.findByRole("heading", { name: "Deprecated, still used" });
    const captions = screen.getAllByText("used by counts packages in this catalog");
    expect(captions).toHaveLength(2);   // Most used, and the deprecated ones in Worth a look
    const columns = [...document.querySelectorAll(".home-columns > div")];
    expect(columns.map((column) => column.querySelectorAll(".home-note").length)).toEqual([2, 0, 1]);
  });

  it("writes a singular and the shared ecosystem label in This week", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...WEEK,
      totals: { ...WEEK.totals, attention: 1 },
      packages: [{ ...WEEK.packages[0], ecosystem: "pypi", name: "flask", purl: "pkg:pypi/flask" }],
    });
    renderHome();
    expect(await screen.findByText("1 needs attention")).toBeInTheDocument();
    const row = screen.getByRole("link", { name: "flask" }).closest("li");
    expect(within(row).getByText("PyPI")).toHaveClass("tag");
    expect(within(row).queryByText("pypi")).not.toBeInTheDocument();
  });

  it("omits a Worth a look subheading with nothing to show", async () => {
    api.fetchCatalogSummary.mockResolvedValue({
      ...CATALOG,
      deprecated_in_use: [],
      stale_in_use: { ...CATALOG.stale_in_use, packages: 0 },
    });
    renderHome();
    await screen.findByRole("list", { name: "Ecosystems" });
    expect(screen.queryByRole("heading", { name: "Deprecated, still used" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /No release in/ })).not.toBeInTheDocument();
  });

  it("says when nothing needs attention", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({ ...WEEK, totals: { ...WEEK.totals, attention: 0 }, packages: [WEEK.packages[1]] });
    renderHome();
    expect(await screen.findByText("Nothing needs attention this week")).toBeInTheDocument();
    expect(screen.getByText(/0 need attention/)).toBeInTheDocument();
  });

  it("names a failed and a stale source and styles the line as a warning", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...WEEK,
      last_runs: [
        run("pypi"),
        run("npm", { status: "failed" }),
        run("crates", { started_at: finished(100), finished_at: finished(96) }),
      ],
    });
    renderHome();
    const line = await screen.findByText(/1 of 3 sources updated/);
    expect(line).toHaveClass("warn-text");
    expect(line.textContent).toContain("npm (failed)");
    expect(line.textContent).toContain("crates (no run for 4 days)");
  });

  it("keeps the other blocks when the catalog summary fails", async () => {
    api.fetchCatalogSummary.mockRejectedValue({ message: "HTTP 500" });
    renderHome();
    expect((await screen.findAllByText(/Could not load .*: HTTP 500/)).length).toBeGreaterThanOrEqual(4);
    expect(await screen.findByText(/7 of 7 sources updated/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "left-pad" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name or purl")).toBeInTheDocument();
  });

  it("keeps the lists when the updates summary fails", async () => {
    api.fetchUpdatesSummary.mockRejectedValue({ message: "HTTP 503" });
    renderHome();
    expect(await screen.findByText("Could not load the update status: HTTP 503")).toBeInTheDocument();
    expect(screen.getByText("Could not load this week's updates: HTTP 503")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "golang.org/x/sys" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Ecosystems" })).toBeInTheDocument();
  });

  it("goes to the search page with the query on Enter", async () => {
    const user = userEvent.setup();
    renderHome();
    await user.type(screen.getByLabelText("Name or purl"), "left pad{Enter}");
    expect(await screen.findByTestId("where")).toHaveTextContent("/search?q=left%20pad");
    expect(api.searchPackages).not.toHaveBeenCalledWith(expect.objectContaining({ limit: 2 }));
  });

  it("opens the card when a purl names exactly one package", async () => {
    const user = userEvent.setup();
    api.searchPackages.mockResolvedValue({
      data: [{ ...item("express"), ecosystem: "npm", purl: "pkg:npm/express" }],
      cursor: null,
      has_more: false,
    });
    renderHome();
    await user.type(screen.getByLabelText("Name or purl"), "pkg:npm/express{Enter}");
    expect(await screen.findByTestId("where")).toHaveTextContent("/package/npm/express");
    expect(api.searchPackages).toHaveBeenCalledWith({ q: "pkg:npm/express", limit: 2 });
  });

  it("carries the version of a purl to the card", async () => {
    const user = userEvent.setup();
    api.searchPackages.mockResolvedValue({
      data: [{ ...item("express"), ecosystem: "npm", purl: "pkg:npm/express" }],
      cursor: null,
      has_more: false,
    });
    renderHome();
    await user.type(screen.getByLabelText("Name or purl"), "pkg:npm/express@4.18.2{Enter}");
    expect(await screen.findByTestId("where")).toHaveTextContent("/package/npm/express?version=4.18.2");
  });

  it("sends a purl that matches nothing to the search page", async () => {
    const user = userEvent.setup();
    renderHome();
    await user.type(screen.getByLabelText("Name or purl"), "pkg:npm/nope{Enter}");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/search?q=pkg%3Anpm%2Fnope"));
  });
});

describe("App routes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchCatalogSummary.mockResolvedValue(CATALOG);
    api.fetchUpdatesSummary.mockResolvedValue(WEEK);
    api.fetchPackages.mockResolvedValue({ data: [], cursor: null, has_more: false });
    api.searchPackages.mockResolvedValue({ data: [], cursor: null, has_more: false });
  });

  function renderApp(url) {
    return render(
      <MemoryRouter initialEntries={[url]} future={FUTURE}>
        <App />
        <LocationProbe />
      </MemoryRouter>
    );
  }

  it("redirects an old query on / to /search and keeps the query", async () => {
    renderApp("/?q=flask&ecosystem=pypi");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/search?q=flask&ecosystem=pypi"));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenCalledWith(expect.objectContaining({ q: "flask", ecosystem: "pypi", limit: 20 }));
    });
    expect(screen.getByRole("heading", { name: "Search packages" })).toBeInTheDocument();
  });

  it("shows the home page on / and the header navigation in order", async () => {
    renderApp("/");
    expect(await screen.findByLabelText("Name or purl")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "PackageHub" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation");
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual(["Catalog", "Updates", "Resolve"]);
  });
});
