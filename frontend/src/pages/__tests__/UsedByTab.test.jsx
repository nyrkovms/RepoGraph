// The Used by tab of the package card (SPEC-CATALOG-PAGES.md §5).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import PackagePage from "../PackagePage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchArtifacts: vi.fn(),
  fetchDependencies: vi.fn(),
  fetchDependents: vi.fn(),
  fetchOverview: vi.fn(),
  fetchPackage: vi.fn(),
  fetchVersion: vi.fn(),
  fetchVersions: vi.fn(),
  fetchVulnerabilities: vi.fn(),
}));

const MONTHS = Array.from({ length: 12 }, (_, index) => `2026-${String(index + 1).padStart(2, "0")}`);

function overviewOf(version, patch = {}) {
  return {
    version,
    dependencies: { direct: 4, optional: 2, dev: 0, indirect: 3, not_in_catalog: 1 },
    used_by: { direct: 1, indirect: 2 },
    licenses: [
      { license: "MIT", count: 3 },
      { license: "PSF-2.0", count: 1 },
    ],
    versions: { total: 211, prereleases: 36, not_active: 2 },
    activity: MONTHS.map((month, index) => ({ month, releases: index === 10 ? 19 : 0 })),
    lines: [
      { line: "2.14", latest: "2.14.3", released_at: "2026-11-05T09:00:00Z", stable_releases: 4, started_at: "2026-04-02T09:00:00Z" },
      { line: "2.13", latest: "2.13.5", released_at: "2026-09-01T09:00:00Z", stable_releases: 5, started_at: "2026-01-02T09:00:00Z" },
      { line: "2.12", latest: "2.12.0", released_at: "2026-06-01T09:00:00Z", stable_releases: 1, started_at: "2026-06-01T09:00:00Z" },
      { line: "1.10", latest: "1.10.22", released_at: "2025-01-01T09:00:00Z", stable_releases: 22, started_at: "2024-01-01T09:00:00Z" },
    ],
    ...patch,
  };
}

const PYDANTIC = {
  ecosystem: "pypi",
  name: "pydantic",
  normalized_name: "pydantic",
  namespace: "",
  purl: "pkg:pypi/pydantic",
  description: "Data validation using Python type hints",
  homepage_url: "https://docs.pydantic.dev",
  repository_url: "https://github.com/pydantic/pydantic",
  documentation_url: null,
  license_raw: "MIT",
  license_spdx: "MIT",
  latest_version: "2.14.3",
  sources: [],
};

const VERSIONS = [
  { version: "2.14.3", status: "active", release_date: "2026-11-05T09:00:00Z" },
  { version: "2.13.5", status: "active", release_date: "2026-09-01T09:00:00Z" },
  { version: "2.0.0", status: "yanked", release_date: "2024-01-01T09:00:00Z" },
];

function LocationProbe() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderCard(path = "/package/pydantic") {
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <LocationProbe />
      <Routes>
        <Route path="/package/:name" element={<PackagePage />} />
        <Route path="/package/:ecosystem/*" element={<PackagePage />} />
      </Routes>
    </MemoryRouter>
  );
}

const query = () => Object.fromEntries(new URLSearchParams(screen.getByTestId("search").textContent));

describe("used by tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchPackage.mockResolvedValue(PYDANTIC);
    api.fetchVersions.mockResolvedValue({ data: VERSIONS, cursor: null, has_more: false });
    api.fetchVulnerabilities.mockResolvedValue({ package: "pydantic", coverage: "collected", data: [] });
    api.fetchVersion.mockImplementation((ecosystem, name, version) =>
      Promise.resolve({
        name,
        version,
        purl: `pkg:pypi/pydantic@${version}`,
        release_date: VERSIONS.find((row) => row.version === version)?.release_date ?? null,
        status: VERSIONS.find((row) => row.version === version)?.status ?? "active",
        dependencies_state: "hydrated",
        sources: [{ registry: "pypi", enabled: true, status: "active", first_seen_at: "2026-01-01T00:00:00Z" }],
      })
    );
    api.fetchDependencies.mockResolvedValue({
      package: "pydantic",
      version: "2.14.3",
      depth: 1,
      dependencies: [
        { name: "pydantic-core", constraint: "==2.46.5", dependency_type: "runtime", extra_name: null, environment_markers: null, resolved_version: "2.46.5", cycle: false, children: [] },
      ],
    });
    api.fetchOverview.mockImplementation((ecosystem, name, version) => Promise.resolve(overviewOf(version)));
  });

  // An Overview section by its heading.
  const section = (name) => screen.getByRole("heading", { name }).closest(".overview-section");

  async function ready() {
    await screen.findByRole("tab", { name: /Used by/ });
    await screen.findByRole("tab", { name: /Versions\s*211/ });
  }

  const direct = (name, patch = {}) => ({
    ecosystem: "npm", namespace: "", name, normalized_name: name, purl: `pkg:npm/${name}`, latest_version: "1.2.3", relation: "direct",
    dependency_types: ["runtime"], requires: ["^1.5"], ...patch,
  });
  const indirect = (name) => ({
    ecosystem: "go", namespace: "", name, normalized_name: name, purl: `pkg:golang/${name}`, latest_version: "v2.0.0", relation: "indirect",
    dependency_types: [], requires: [],
  });
  const TOTALS = { direct: 1, indirect: 2 };
  const ROWS = [direct("left-pad", { dependency_types: ["peer", "runtime"], requires: ["^1.5", "^2"] }), indirect("github.com/a/b"), indirect("github.com/c/d")];

  beforeEach(() => {
    api.fetchDependents.mockImplementation((e, n, params) =>
      Promise.resolve({
        totals: TOTALS,
        data: ROWS.filter((row) => !params.relation || row.relation === params.relation),
        cursor: null,
        has_more: false,
      })
    );
  });

  it("puts the tab between Dependencies and Versions with the total as its count", async () => {
    renderCard();
    await ready();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Overview", "Dependencies4", "Used by1", "Versions211", "Compare", "Sources"]);
  });

  it("does not ask for the list until the tab is opened, then asks once per package", async () => {
    const user = userEvent.setup();
    renderCard();
    await ready();
    expect(api.fetchDependents).not.toHaveBeenCalled();

    await user.click(screen.getByRole("tab", { name: /Used by/ }));
    await screen.findByText("left-pad");
    expect(api.fetchDependents).toHaveBeenCalledTimes(1);
    expect(api.fetchDependents).toHaveBeenCalledWith("pypi", "pydantic", { relation: "", limit: 50, cursor: undefined });

    await user.click(screen.getByRole("tab", { name: /Versions/ }));
    await user.click(screen.getByRole("tab", { name: /Used by/ }));
    await user.selectOptions(screen.getByLabelText("Version"), "2.13.5");
    expect(await screen.findByText("left-pad")).toBeInTheDocument();
    expect(api.fetchDependents).toHaveBeenCalledTimes(1);
  });

  it("has no heading that repeats the tab name", async () => {
    renderCard("/package/pydantic?tab=used-by");
    await screen.findByText("left-pad");
    expect(screen.queryByRole("heading", { name: "Used by" })).not.toBeInTheDocument();
  });

  it("opens from the tab and from the View all link of the Overview", async () => {
    const user = userEvent.setup();
    renderCard("/package/pydantic?version=2.13.5");
    await ready();
    const link = within(section("Used by, in this catalog")).getByRole("link", { name: "View all" });
    expect(link).toHaveAttribute("href", "?version=2.13.5&tab=used-by");
    await user.click(link);
    expect(screen.getByRole("tab", { name: /Used by/ })).toHaveAttribute("aria-selected", "true");
    expect(query().tab).toBe("used-by");
  });

  it("keeps the section plain text when nothing depends on the package", async () => {
    api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version, { used_by: { direct: 0, indirect: 0 } })));
    renderCard();
    await ready();
    expect(within(section("Used by, in this catalog")).queryByRole("link")).not.toBeInTheDocument();
    expect(document.querySelector(".package-facts")).not.toHaveTextContent("in catalog");
  });

  it("shows a row per dependent with its version, requirement, type and relation", async () => {
    renderCard("/package/pydantic?tab=used-by");
    await screen.findByText("left-pad");
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows).toHaveLength(4);
    const cells = (row) => within(row).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells(rows[1])).toEqual(["left-pad", "1.2.3", "^1.5, ^2", "peer, runtime", "Direct"]);
    expect(cells(rows[2])).toEqual(["github.com/a/b", "v2.0.0", "—", "—", "Indirect"]);
    expect(within(rows[1]).getByRole("link", { name: "left-pad" })).toHaveAttribute("href", "/package/npm/left-pad");
    expect(within(rows[2]).getByRole("link", { name: "github.com/a/b" })).toHaveAttribute("href", "/package/go/github.com/a/b");
    expect(screen.getByText("Counted among latest versions in this catalog: runtime, peer and provided dependencies. It shows what is affected here, not how popular the package is.")).toBeInTheDocument();
  });

  it("shows the native name of a scoped, Maven and RPM dependent", async () => {
    api.fetchDependents.mockResolvedValue({
      totals: { direct: 3, indirect: 0 },
      data: [
        direct("@angular/core", { namespace: "angular", normalized_name: "core", purl: "pkg:npm/%40angular/core" }),
        direct("org.x:lib", { ecosystem: "maven", namespace: "org.x", normalized_name: "lib", purl: "pkg:maven/org.x/lib" }),
        direct("openssl", { ecosystem: "rpm", namespace: "rocky-9", purl: "pkg:rpm/rocky/openssl" }),
      ],
      cursor: null, has_more: false,
    });
    renderCard("/package/pydantic?tab=used-by");
    expect(await screen.findByRole("link", { name: "@angular/core" })).toHaveAttribute("href", "/package/npm/@angular/core");
    expect(screen.getByRole("link", { name: "org.x:lib" })).toHaveAttribute("href", "/package/maven/org.x:lib");
    expect(screen.getByRole("link", { name: "openssl" })).toHaveAttribute("href", "/package/rpm/rocky-9/openssl");
  });

  it("filters by relation through the chips and keeps the filter in the address", async () => {
    const user = userEvent.setup();
    renderCard("/package/pydantic?tab=used-by");
    await screen.findByText("left-pad");
    expect(screen.getByRole("link", { name: "All 3" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "Direct 1" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Indirect 2" }));
    await waitFor(() => expect(query().relation).toBe("indirect"));
    expect(api.fetchDependents).toHaveBeenLastCalledWith("pypi", "pydantic", { relation: "indirect", limit: 50, cursor: undefined });
    await waitFor(() => expect(screen.queryByText("left-pad")).not.toBeInTheDocument());
    expect(screen.getByText("github.com/a/b")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "All 3" }));
    await waitFor(() => expect(query().relation).toBeUndefined());
    expect(query().tab).toBe("used-by");
  });

  it("restores the filter from ?relation=", async () => {
    renderCard("/package/pydantic?tab=used-by&relation=direct");
    await screen.findByText("left-pad");
    expect(api.fetchDependents).toHaveBeenCalledWith("pypi", "pydantic", { relation: "direct", limit: 50, cursor: undefined });
    expect(screen.getByRole("link", { name: "Direct 1" })).toHaveAttribute("aria-current", "true");
    expect(screen.queryByText("github.com/a/b")).not.toBeInTheDocument();
  });

  it("loads more with the cursor and hides the button at the end", async () => {
    const user = userEvent.setup();
    api.fetchDependents.mockImplementation((e, n, params) =>
      Promise.resolve(
        params.cursor
          ? { totals: TOTALS, data: [indirect("github.com/c/d")], cursor: null, has_more: false }
          : { totals: TOTALS, data: [ROWS[0], ROWS[1]], cursor: "next-page", has_more: true }
      )
    );
    renderCard("/package/pydantic?tab=used-by");
    await screen.findByText("left-pad");
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(3);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("github.com/c/d");
    expect(api.fetchDependents).toHaveBeenLastCalledWith("pypi", "pydantic", { relation: "", limit: 50, cursor: "next-page" });
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("says nothing depends on the package when the list is empty", async () => {
    api.fetchDependents.mockResolvedValue({ totals: { direct: 0, indirect: 0 }, data: [], cursor: null, has_more: false });
    renderCard("/package/pydantic?tab=used-by");
    expect(await screen.findByText("No packages in this catalog depend on it", { selector: "p:not(.muted)" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows a message in the tab when the list cannot be loaded", async () => {
    api.fetchDependents.mockRejectedValue({ code: "http_error", message: "HTTP 500", status: 500 });
    renderCard("/package/pydantic?tab=used-by");
    expect(await screen.findByText("Used by is not available")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Versions/ })).toBeInTheDocument();
  });

  it("does not say nothing depends on the package when only a filter is empty", async () => {
    api.fetchDependents.mockImplementation((e, n, params) =>
      Promise.resolve({ totals: { direct: 4, indirect: 0 }, data: params.relation === "indirect" ? [] : [direct("left-pad")], cursor: null, has_more: false })
    );
    renderCard("/package/pydantic?tab=used-by&relation=indirect");
    expect(await screen.findByText("No indirect dependents in this catalog")).toBeInTheDocument();
    expect(screen.queryByText("No packages in this catalog depend on it")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Direct 4" })).toBeInTheDocument();
  });

  it("names an empty direct filter as direct", async () => {
    api.fetchDependents.mockResolvedValue({ totals: { direct: 0, indirect: 2 }, data: [], cursor: null, has_more: false });
    renderCard("/package/pydantic?tab=used-by&relation=direct");
    expect(await screen.findByText("No direct dependents in this catalog")).toBeInTheDocument();
  });

  it("counts the direct dependents on the tab, as the header fact and the zero state do", async () => {
    renderCard();
    await ready();
    expect(screen.getByRole("tab", { name: /Used by/ })).toHaveTextContent("Used by1");
    expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveTextContent("Dependencies4");
  });

  describe("RPM", () => {
    const NOTE = "Dependencies on capabilities are not counted";
    const rpmOverview = (version, patch = {}) =>
      overviewOf(version, {
        dependencies: { direct: 6, optional: 1, dev: 0, indirect: null, not_in_catalog: null },
        used_by: { direct: 0, indirect: null },
        licenses: [],
        lines: [],
        ...patch,
      });

    beforeEach(() => {
      api.fetchPackage.mockResolvedValue({ ...PYDANTIC, ecosystem: "rpm", namespace: "rocky-9", name: "rocky-9/bash", normalized_name: "bash", purl: "pkg:rpm/rocky/bash?distro=rocky-9" });
      api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(rpmOverview(version)));
      api.fetchArtifacts.mockResolvedValue({ data: [] });
    });

    it("shows what is counted, hides what is not, and says why on the Overview", async () => {
      renderCard("/package/rpm/rocky-9/bash");
      await screen.findByRole("tab", { name: /Versions\s*211/ });
      expect(screen.getByRole("tab", { name: /Used by/ })).toHaveTextContent("Used by0");
      expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveTextContent("Dependencies6");
      // The summary sentence has direct numbers only, and the Used by one for RPM says "directly".
      expect(document.querySelector(".overview-summary")).toHaveTextContent(
        "Depends on 6 packages directly. No packages in this catalog use it."
      );
      const block = section;
      expect(within(block("Dependencies")).getByText("Direct")).toBeInTheDocument();
      expect(within(block("Dependencies")).queryByText("Indirect")).not.toBeInTheDocument();
      expect(block("Dependencies")).not.toHaveTextContent("not in catalog");
      expect(block("Dependencies")).not.toHaveTextContent("lower bound");
      expect(block("Used by, in this catalog")).toHaveTextContent(NOTE);
      expect(block("Used by, in this catalog")).not.toHaveTextContent("No packages in this catalog depend on it");
      expect(block("Licenses")).not.toHaveTextContent("Dependencies:");
      expect(block("Licenses")).not.toHaveTextContent("No dependency in this catalog");
    });

    it("keeps the Overview note next to the direct bar when something depends on the package by name", async () => {
      api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(rpmOverview(version, { used_by: { direct: 3, indirect: null } })));
      renderCard("/package/rpm/rocky-9/bash");
      await screen.findByRole("tab", { name: /Versions\s*211/ });
      expect(within(section("Used by, in this catalog")).getByRole("link", { name: "View all" })).toBeInTheDocument();
      expect(screen.getByText(NOTE)).toBeInTheDocument();
      expect(document.querySelector(".overview-summary")).toHaveTextContent("3 packages in this catalog use it directly.");
    });

    it("lists direct dependents only, without relation chips, under the note", async () => {
      renderCard("/package/rpm/rocky-9/bash?tab=used-by");
      await screen.findByText("left-pad");
      expect(api.fetchDependents).toHaveBeenCalledWith("rpm", "rocky-9/bash", { relation: "direct", limit: 50, cursor: undefined });
      expect(screen.getByText(NOTE)).toBeInTheDocument();
      expect(screen.queryByRole("navigation", { name: "Relation" })).not.toBeInTheDocument();
    });

    it("says the zero state is not a finding", async () => {
      api.fetchDependents.mockResolvedValue({ totals: { direct: 0, indirect: 0 }, data: [], cursor: null, has_more: false });
      renderCard("/package/rpm/rocky-9/bash?tab=used-by");
      expect(await screen.findByText(NOTE)).toBeInTheDocument();
      expect(screen.queryByText("No packages in this catalog depend on it")).not.toBeInTheDocument();
    });
  });
});
