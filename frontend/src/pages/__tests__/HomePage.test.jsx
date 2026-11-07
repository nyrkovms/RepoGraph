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
  most_used: [
    item("golang.org/x/sys", { used_by: 50, description: "Go packages for low-level system calls" }),
    item("github.com/stretchr/testify", { used_by: 45 }),
  ],
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
      description: "A web framework",
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

  it("shows the search box, ecosystems, status, this week and the two lists", async () => {
    renderHome();

    const input = screen.getByLabelText("Name or purl");
    expect(input).toHaveAttribute("placeholder", "requests, pkg:npm/express");

    const chips = await screen.findByRole("list", { name: "Ecosystems" });
    const buttons = within(chips).getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual(["All 559", "RPM 218", "Go 214", "crates.io 127"]);
    expect(buttons[0]).toHaveAttribute("aria-pressed", "true");
    expect(buttons[1]).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("link", { name: /Browse all/ })).not.toBeInTheDocument();

    expect(await screen.findByText(/7 of 7 sources updated · last run/)).toBeInTheDocument();
    expect(screen.getByText("Security advisories: PyPI only")).toBeInTheDocument();
    expect(api.fetchUpdatesSummary).toHaveBeenCalledWith(expect.objectContaining({ period: "week" }));
    expect(api.fetchCatalogSummary).toHaveBeenCalledWith({ ecosystem: "" });

    expect(screen.getByText(/2 need attention/)).toBeInTheDocument();
    expect(screen.getByText(/41 packages updated/)).toBeInTheDocument();
    expect(screen.getByText(/77\s+releases/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All updates this week" })).toHaveAttribute("href", "/updates?period=week");

    // Needs attention: only the flagged package, with the reason and both actions.
    expect(screen.getByRole("heading", { name: "Needs attention" })).toBeInTheDocument();
    const attention = screen.getByRole("link", { name: "left-pad" });
    expect(attention).toHaveAttribute("href", "/package/npm/left-pad");
    const row = attention.closest("li");
    expect(within(row).getByText("yanked")).toBeInTheDocument();
    expect(within(row).getByText("major")).toBeInTheDocument();
    expect(within(row).getByText("A release was withdrawn by its publisher.")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Compare 1.2.0 → 2.0.0" })).toHaveAttribute(
      "href",
      "/package/npm/left-pad?version=2.0.0&tab=compare&from=1.2.0"
    );
    expect(within(row).getByRole("link", { name: "Who uses it (9)" })).toHaveAttribute("href", "/package/npm/left-pad?tab=used-by");

    // Updated, most used first: the unflagged one, with its bump, description and usage.
    expect(screen.getByRole("heading", { name: "Updated, most used first" })).toBeInTheDocument();
    const updated = screen.getByRole("link", { name: "flask" }).closest("li");
    expect(within(updated).getByText("minor")).toBeInTheDocument();
    expect(within(updated).getByText("A web framework")).toBeInTheDocument();
    expect(within(updated).getByText("used by 3 packages")).toBeInTheDocument();

    expect(screen.queryByRole("heading", { name: "Latest releases" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Most used in catalog" })).toBeInTheDocument();
    expect(screen.getByText("Packages the rest of the catalog depends on")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "golang.org/x/sys" })).toHaveAttribute("href", "/package/go/golang.org/x/sys");
    expect(screen.getByText("50 dependents")).toBeInTheDocument();
    expect(screen.getByText("Go packages for low-level system calls")).toBeInTheDocument();
    // No description: the latest version stands in.
    expect(screen.getAllByText("latest v1.0.0", { selector: ".row-description" }).length).toBeGreaterThan(0);
    expect(screen.getByText("Go counts test dependencies too: go.mod does not separate them.")).toBeInTheDocument();
    expect(screen.queryByText("used by counts packages in this catalog")).not.toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Worth a look" })).toBeInTheDocument();
    expect(screen.getByText("Deprecated, but other packages still use them")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "github.com/golang/protobuf" })).toBeInTheDocument();
    expect(screen.getByText("used by 5")).toBeInTheDocument();
    const stale = screen.getByRole("link", {
      name: "39 packages had no release in 3+ years and are used by 5 or more",
    });
    const target = new URL(stale.getAttribute("href"), "http://x");
    expect(target.pathname).toBe("/search");
    expect(Object.fromEntries(target.searchParams)).toEqual({
      released_before: "2023-11-07T00:00:00Z",
      min_used_by: "5",
      sort: "used_by",
    });
  });

  it("shows at most five updated packages without flags and skips prerelease-only ones", async () => {
    const flat = (name, extra = {}) => ({ ...WEEK.packages[1], name, purl: `pkg:pypi/${name}`, ...extra });
    api.fetchUpdatesSummary.mockResolvedValue({
      ...WEEK,
      packages: [
        flat("beta", { prerelease_only: true }),
        ...["a", "b", "c", "d", "e", "f", "g"].map((name) => flat(name)),
      ],
    });
    renderHome();
    const heading = await screen.findByRole("heading", { name: "Updated, most used first" });
    const list = heading.nextElementSibling;
    expect(within(list).getAllByRole("listitem").map((li) => within(li).getAllByRole("link")[0].textContent)).toEqual([
      "a",
      "b",
      "c",
      "d",
      "e",
    ]);
  });

  it("hides used by at zero and the Nothing depends text appears on an attention row without dependents", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...WEEK,
      packages: [
        { ...WEEK.packages[0], used_by: 0 },
        { ...WEEK.packages[1], used_by: 0 },
      ],
    });
    renderHome();
    await screen.findByRole("link", { name: "flask" });
    expect(screen.queryByText(/^used by \d+ package/)).not.toBeInTheDocument();
    expect(screen.getByText("Nothing in this catalog depends on it")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Who uses it/ })).not.toBeInTheDocument();
  });

  it("lists up to five deprecated packages and links to the rest only when there are more", async () => {
    const many = Array.from({ length: 5 }, (_, index) => item(`github.com/dep/p${index}`, { used_by: 9 - index }));
    api.fetchCatalogSummary.mockResolvedValue({ ...CATALOG, deprecated_in_use: many, deprecated_in_use_total: 12 });
    renderHome();
    const more = await screen.findByRole("link", { name: "All 12 deprecated packages in use" });
    expect(more).toHaveAttribute("href", "/search?deprecated=true&min_used_by=1&sort=used_by");
    expect(screen.getAllByText(/^used by \d$/)).toHaveLength(5);
  });

  it("has no link to the rest when every deprecated package is listed", async () => {
    api.fetchCatalogSummary.mockResolvedValue({ ...CATALOG, deprecated_in_use_total: 1 });
    renderHome();
    await screen.findByRole("heading", { name: "Worth a look" });
    expect(screen.queryByRole("link", { name: /deprecated packages in use/ })).not.toBeInTheDocument();
  });

  it("puts the subtitles in two columns and no used by caption", async () => {
    renderHome();
    await screen.findByRole("heading", { name: "Worth a look" });
    const columns = [...document.querySelectorAll(".home-columns > div")];
    expect(columns).toHaveLength(2);
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

  it("says nothing stands out when Worth a look has nothing to show", async () => {
    api.fetchCatalogSummary.mockResolvedValue({
      ...CATALOG,
      deprecated_in_use: [],
      stale_in_use: { ...CATALOG.stale_in_use, packages: 0 },
    });
    renderHome();
    expect(await screen.findByText("Nothing stands out")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /had no release/ })).not.toBeInTheDocument();
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
    expect((await screen.findAllByText(/Could not load .*: HTTP 500/)).length).toBeGreaterThanOrEqual(3);
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

  describe("ecosystem chips", () => {
    function renderAt(entry) {
      return render(
        <MemoryRouter initialEntries={[entry]} future={FUTURE}>
          <Routes>
            <Route
              path="/"
              element={
                <>
                  <HomePage />
                  <LocationProbe />
                </>
              }
            />
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      );
    }

    it("filter in place: the address, the requests and the browse link follow the chip", async () => {
      const user = userEvent.setup();
      renderAt("/");
      await screen.findByRole("list", { name: "Ecosystems" });
      await user.click(screen.getByRole("button", { name: "Go 214" }));

      expect(screen.getByTestId("where")).toHaveTextContent("/?ecosystem=go");
      expect(screen.getByRole("button", { name: "Go 214" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByRole("button", { name: /^All/ })).toHaveAttribute("aria-pressed", "false");
      await waitFor(() => expect(api.fetchCatalogSummary).toHaveBeenLastCalledWith({ ecosystem: "go" }));
      expect(api.fetchUpdatesSummary).toHaveBeenLastCalledWith(expect.objectContaining({ period: "week", ecosystem: "go" }));
      expect(screen.getByRole("link", { name: "Browse all 214 Go packages" })).toHaveAttribute("href", "/search?ecosystem=go");
      expect(screen.getByRole("link", { name: "All updates this week" })).toHaveAttribute(
        "href",
        "/updates?period=week&ecosystem=go"
      );
      const stale = screen.getByRole("link", { name: /had no release in 3\+ years/ });
      expect(new URL(stale.getAttribute("href"), "http://x").searchParams.get("ecosystem")).toBe("go");

      await user.click(screen.getByRole("button", { name: /^All/ }));
      expect(screen.getByTestId("where")).toHaveTextContent(/^\/$/);
      expect(screen.queryByRole("link", { name: /Browse all/ })).not.toBeInTheDocument();
    });

    it("passes the ecosystem to the search box and to the search page", async () => {
      const user = userEvent.setup();
      renderAt("/?ecosystem=go");
      await screen.findByRole("list", { name: "Ecosystems" });
      await user.type(screen.getByLabelText("Name or purl"), "spew");
      await waitFor(() => expect(api.searchPackages).toHaveBeenCalledWith(expect.objectContaining({ q: "spew", ecosystem: "go" })));
      await user.type(screen.getByLabelText("Name or purl"), "{Enter}");
      await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/search?q=spew&ecosystem=go"));
    });
  });

  it("goes to the search page with the query on Enter", async () => {
    const user = userEvent.setup();
    renderHome();
    await user.type(screen.getByLabelText("Name or purl"), "left pad{Enter}");
    expect(await screen.findByTestId("where")).toHaveTextContent("/search?q=left%20pad");
    expect(api.searchPackages).not.toHaveBeenCalledWith(expect.objectContaining({ limit: 2 }));
  });

  it("runs the search on Enter even when suggestions are showing", async () => {
    const user = userEvent.setup();
    api.searchPackages.mockResolvedValue({
      data: [{ ...item("left-pad"), ecosystem: "npm", purl: "pkg:npm/left-pad", normalized_name: "left-pad" }],
      cursor: null,
      has_more: false,
    });
    renderHome();
    await user.type(screen.getByLabelText("Name or purl"), "left");
    await screen.findByRole("listbox", { name: "Search suggestions" });
    await user.keyboard("{Enter}");
    expect(await screen.findByTestId("where")).toHaveTextContent("/search?q=left");
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

  it("redirects ecosystem plus a query but keeps ecosystem alone on the home page", async () => {
    renderApp("/?ecosystem=go&q=spew");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/search?ecosystem=go&q=spew"));
  });

  it("does not redirect ecosystem alone", async () => {
    renderApp("/?ecosystem=go");
    expect(await screen.findByLabelText("Name or purl")).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("/?ecosystem=go");
    expect(await screen.findByRole("button", { name: "Go 214" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the home page on / and the header navigation in order", async () => {
    renderApp("/");
    expect(await screen.findByLabelText("Name or purl")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "PackageHub" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation");
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual(["Catalog", "Updates", "Resolve"]);
  });
});
