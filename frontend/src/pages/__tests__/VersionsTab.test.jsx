// The Versions tab of the package card: release lines, filters and version rows (SPEC-CATALOG-PAGES.md §5).
import { configure, render, screen, waitFor, within } from "@testing-library/react";
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
  { version: "2.14.3", line: "2.14", is_prerelease: false, status: "active", release_date: "2026-11-05T09:00:00Z" },
  { version: "2.14.2", line: "2.14", is_prerelease: false, status: "yanked", status_reason: "broken wheel", release_date: "2026-10-05T09:00:00Z" },
  { version: "2.13.5", line: "2.13", is_prerelease: false, status: "active", release_date: "2026-09-01T09:00:00Z" },
  { version: "2.13.0b1", line: "2.13", is_prerelease: true, status: "active", release_date: "2026-08-01T09:00:00Z" },
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

// The tab shows nothing until the overview is there, which comes after the tree: allow for a loaded machine.
beforeAll(() => configure({ asyncUtilTimeout: 4000 }));
afterAll(() => configure({ asyncUtilTimeout: 1000 }));

describe("versions tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchPackage.mockResolvedValue(PYDANTIC);
    api.fetchVersions.mockImplementation((e, n, params) => {
      if (params.line) {
        return Promise.resolve({ data: VERSIONS.filter((row) => row.line === params.line), cursor: null, has_more: false });
      }
      if (params.prerelease) {
        return Promise.resolve({ data: VERSIONS.filter((row) => row.is_prerelease), cursor: null, has_more: false });
      }
      if (params.not_active) {
        return Promise.resolve({ data: VERSIONS.filter((row) => row.status !== "active"), cursor: null, has_more: false });
      }
      return Promise.resolve({ data: VERSIONS, cursor: null, has_more: false });
    });
    api.fetchVulnerabilities.mockResolvedValue({ package: "pydantic", coverage: "collected", data: [] });
    api.fetchVersion.mockImplementation((ecosystem, name, version) => {
      const row = VERSIONS.find((item) => item.version === version);
      return Promise.resolve({
        name,
        version,
        purl: `pkg:pypi/pydantic@${version}`,
        release_date: row?.release_date ?? null,
        status: row?.status ?? "active",
        line: row?.line ?? null,
        dependencies_state: "hydrated",
        sources: [],
      });
    });
    api.fetchDependencies.mockResolvedValue({ package: "pydantic", version: "2.14.3", depth: 0, dependencies: [] });
    api.fetchArtifacts.mockResolvedValue({ data: [] });
    api.fetchOverview.mockImplementation((ecosystem, name, version) => Promise.resolve(overviewOf(version)));
  });

  const chipNames = () => within(screen.getByRole("navigation", { name: "Versions view" })).getAllByRole("link").map((link) => link.textContent);
  const lineRows = () => within(screen.getByRole("table")).getAllByRole("row");

  it("shows the lines as a table in the order of the API, with the chips and their counts", async () => {
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");
    expect(chipNames()).toEqual(["Lines", "Prereleases 36", "Not active 2", "All 211"]);
    expect(screen.getByRole("link", { name: "Lines" })).toHaveAttribute("aria-current", "true");
    const cells = (row) => within(row).getAllByRole("cell").map((cell) => cell.textContent);
    expect(within(lineRows()[0]).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Line", "Latest", "Released", "Stable releases", "Started",
    ]);
    expect(cells(lineRows()[1])).toEqual(["2.14viewing", "2.14.3", "2026-11-05", "4", "2026-04-02"]);
    expect(cells(lineRows()[4])).toEqual(["1.10", "1.10.22", "2025-01-01", "22", "2024-01-01"]);
    // The first page of versions is the one the page loaded; no line was asked for.
    expect(api.fetchVersions.mock.calls.every(([, , params]) => !params.line)).toBe(true);
    expect(query().view).toBeUndefined();
  });

  it("expands a line on first click, asks for it with line=, keeps it in ?line= and collapses it again", async () => {
    const user = userEvent.setup();
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");

    await user.click(screen.getByRole("link", { name: "2.13" }));
    await waitFor(() => expect(query().line).toBe("2.13"));
    expect(api.fetchVersions).toHaveBeenCalledWith("pypi", "pydantic", { line: "2.13", cursor: undefined });
    expect(await screen.findByRole("link", { name: "2.13.5" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "2.13.0b1" })).toBeInTheDocument();
    expect(screen.getByText("prerelease", { selector: ".tag" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "2.14.3" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "2.13" }));
    await waitFor(() => expect(query().line).toBeUndefined());
    expect(screen.queryByRole("link", { name: "2.13.5" })).not.toBeInTheDocument();

    // Opened again, it comes from memory.
    await user.click(screen.getByRole("link", { name: "2.13" }));
    expect(await screen.findByRole("link", { name: "2.13.5" })).toBeInTheDocument();
    expect(api.fetchVersions.mock.calls.filter(([, , params]) => params.line === "2.13")).toHaveLength(1);
  });

  it("opens the line named in ?line=", async () => {
    renderCard("/package/pydantic?tab=versions&line=2.14");
    expect(await screen.findByRole("link", { name: "2.14.2" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "2.14" })).toHaveAttribute("aria-expanded", "true");
  });

  it("marks the line of the version being viewed", async () => {
    renderCard("/package/pydantic?tab=versions&version=2.13.5");
    await screen.findByRole("table");
    await waitFor(() => expect(within(lineRows()[2]).getByText("viewing")).toBeInTheDocument());
    expect(within(lineRows()[1]).queryByText("viewing")).not.toBeInTheDocument();
  });

  it("lists the newest 8 lines and the rest behind a button", async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: 10 }, (_, index) => ({
      line: `3.${9 - index}`, latest: `3.${9 - index}.0`, released_at: "2026-01-01T00:00:00Z", stable_releases: 1, started_at: "2026-01-01T00:00:00Z",
    }));
    api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version, { lines: many })));
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");
    expect(lineRows()).toHaveLength(9);
    await user.click(screen.getByRole("button", { name: "Show 2 older lines" }));
    expect(lineRows()).toHaveLength(11);
    expect(screen.queryByRole("button", { name: /older lines/ })).not.toBeInTheDocument();
  });

  it("keeps an expanded older line visible when the address points to it", async () => {
    const many = Array.from({ length: 10 }, (_, index) => ({
      line: `3.${9 - index}`, latest: `3.${9 - index}.0`, released_at: "2026-01-01T00:00:00Z", stable_releases: 1, started_at: "2026-01-01T00:00:00Z",
    }));
    api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version, { lines: many })));
    renderCard("/package/pydantic?tab=versions&line=3.0");
    expect(await screen.findByRole("link", { name: "3.0" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByRole("button", { name: /older lines/ })).not.toBeInTheDocument();
  });

  it("leaves the date cells of a line without dates empty", async () => {
    const lines = [{ line: "0.1", latest: "0.1.0", released_at: null, stable_releases: 2, started_at: null }];
    api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version, { lines })));
    renderCard("/package/pydantic?tab=versions");
    const row = (await screen.findByRole("link", { name: "0.1" })).closest("tr");
    expect([...row.querySelectorAll("td")].map((cell) => cell.textContent)).toEqual(["0.1", "0.1.0", "", "2", ""]);
  });

  it("opens the deprecated versions from their own chip, with the deprecation shown on the row", async () => {
    const user = userEvent.setup();
    api.fetchOverview.mockImplementation((e, n, version) =>
      Promise.resolve(overviewOf(version, { versions: { total: 290, prereleases: 0, not_active: 0, deprecated: 173 } }))
    );
    api.fetchVersions.mockImplementation((e, n, params) =>
      Promise.resolve({
        data: params.deprecated
          ? [{ version: "2.14.2", line: "2.14", is_prerelease: false, status: "active", deprecation: { message: "use 2.14.3" }, release_date: "2026-10-05T09:00:00Z" }]
          : VERSIONS,
        cursor: null,
        has_more: false,
      })
    );
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");
    expect(chipNames()).toEqual(["Lines", "Deprecated 173", "All 290"]);
    await user.click(screen.getByRole("link", { name: "Deprecated 173" }));
    await waitFor(() => expect(query().view).toBe("deprecated"));
    expect(api.fetchVersions).toHaveBeenCalledWith("pypi", "pydantic", { deprecated: true, limit: 50, cursor: undefined });
    const row = (await screen.findByRole("link", { name: "2.14.2" })).closest("li");
    expect(within(row).getByText("deprecated")).toHaveClass("warn");
    expect(within(row).getByText("use 2.14.3")).toBeInTheDocument();
  });

  it("hides a chip with no versions, except All", async () => {
    api.fetchOverview.mockImplementation((e, n, version) =>
      Promise.resolve(overviewOf(version, { versions: { total: 5, prereleases: 0, not_active: 0 } }))
    );
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");
    expect(chipNames()).toEqual(["Lines", "All 5"]);
  });

  it("opens a filter from its chip, keeps it in ?view= and loads more with the cursor", async () => {
    const user = userEvent.setup();
    api.fetchVersions.mockImplementation((e, n, params) => {
      if (params.prerelease) {
        return Promise.resolve(
          params.cursor
            ? { data: [{ ...VERSIONS[3], version: "2.12.0b1", line: "2.12" }], cursor: null, has_more: false }
            : { data: [VERSIONS[3]], cursor: "next-page", has_more: true }
        );
      }
      return Promise.resolve({ data: VERSIONS, cursor: null, has_more: false });
    });
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");

    await user.click(screen.getByRole("link", { name: "Prereleases 36" }));
    await waitFor(() => expect(query().view).toBe("prereleases"));
    expect(api.fetchVersions).toHaveBeenCalledWith("pypi", "pydantic", { prerelease: true, limit: 50, cursor: undefined });
    expect(await screen.findByRole("link", { name: "2.13.0b1" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("link", { name: "2.12.0b1" })).toBeInTheDocument();
    expect(api.fetchVersions).toHaveBeenLastCalledWith("pypi", "pydantic", { prerelease: true, limit: 50, cursor: "next-page" });
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Lines" }));
    await waitFor(() => expect(query().view).toBeUndefined());
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("asks for the versions that are not active with not_active", async () => {
    const user = userEvent.setup();
    renderCard("/package/pydantic?tab=versions");
    await screen.findByRole("table");
    await user.click(screen.getByRole("link", { name: "Not active 2" }));
    await waitFor(() => expect(query().view).toBe("not-active"));
    expect(api.fetchVersions).toHaveBeenCalledWith("pypi", "pydantic", { not_active: true, limit: 50, cursor: undefined });
    expect(await screen.findByRole("link", { name: "2.14.2" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "2.14.3" })).not.toBeInTheDocument();
  });

  it("shows All from the first page the page already has, with a status only where it is not active", async () => {
    renderCard("/package/pydantic?tab=versions&view=all");
    const link = await screen.findByRole("link", { name: "2.14.3" });
    const row = (version) => screen.getByRole("link", { name: version }).closest("li");
    expect(link).toBeInTheDocument();
    expect(within(row("2.14.3")).queryByText("active")).not.toBeInTheDocument();
    expect(within(row("2.14.2")).getByText("yanked")).toHaveClass("warn");
    expect(within(row("2.14.3")).getByText("2026-11-05")).toBeInTheDocument();
    expect(api.fetchVersions).toHaveBeenCalledTimes(1);
  });

  it("opens a package without lines on All, with no Lines chip", async () => {
    api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version, { lines: [] })));
    renderCard("/package/pydantic?tab=versions");
    expect(await screen.findByRole("link", { name: "2.14.3" })).toBeInTheDocument();
    expect(chipNames()).toEqual(["Prereleases 36", "Not active 2", "All 211"]);
    expect(screen.getByRole("link", { name: "All 211" })).toHaveAttribute("aria-current", "true");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("opens an RPM package on All even though the overview lists lines", async () => {
    api.fetchPackage.mockResolvedValue({ ...PYDANTIC, ecosystem: "rpm", name: "openssl", namespace: "rocky-9", latest_version: "3.0.7-1" });
    api.fetchVersions.mockResolvedValue({
      data: [{ version: "3.0.7-1", line: null, is_prerelease: false, status: "active", release_date: "2026-01-01T00:00:00Z" }],
      cursor: null,
      has_more: false,
    });
    renderCard("/package/rpm/rocky-9/openssl?tab=versions");
    expect(await screen.findByRole("link", { name: "3.0.7-1" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Lines" })).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("links a version to the same tab with that version, and moves the viewing marker on a click", async () => {
    const user = userEvent.setup();
    renderCard("/package/pydantic?tab=versions&view=all");
    // The address is rewritten (version first) in a router transition that may land after the rows: the link is built from it.
    await waitFor(() => expect(query().version).toBe("2.14.3"));
    const link = await screen.findByRole("link", { name: "2.13.5" });
    expect(link).toHaveAttribute("href", "?version=2.13.5&tab=versions&view=all");
    await waitFor(() => {
      expect(within(screen.getByRole("link", { name: "2.14.3" }).closest("li")).getByText("viewing")).toBeInTheDocument();
    });

    await user.click(link);
    await waitFor(() => expect(query().version).toBe("2.13.5"));
    expect(query().tab).toBe("versions");
    expect(screen.getByRole("tab", { name: /Versions/ })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => {
      expect(within(screen.getByRole("link", { name: "2.13.5" }).closest("li")).getByText("viewing")).toBeInTheDocument();
    });
    expect(within(screen.getByRole("link", { name: "2.14.3" }).closest("li")).queryByText("viewing")).not.toBeInTheDocument();
  });
});
