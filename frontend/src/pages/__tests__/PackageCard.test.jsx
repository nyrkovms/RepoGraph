// The package card's header, tabs and Overview (SPEC-CATALOG-PAGES.md §5). The tree, versions and sources contents
// are covered by PackagePage.test.jsx.
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

describe("package card", () => {
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

  async function ready() {
    await screen.findByText("Data validation using Python type hints");
    await screen.findByRole("tab", { name: /Versions\s*211/ });
  }

  describe("header", () => {
    it("shows the name, ecosystem, version, latest tag, description, facts and links", async () => {
      renderCard();
      await ready();

      expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("pydantic");
      expect(screen.getByText("PyPI", { selector: ".eco-badge" })).toBeInTheDocument();
      expect(screen.getByLabelText("Version")).toHaveValue("2.14.3");
      expect(screen.getByText("latest", { selector: ".tag" })).toBeInTheDocument();
      expect(screen.queryByText(/Not the latest version/)).not.toBeInTheDocument();

      const facts = document.querySelector(".fact-strip");
      expect(within(facts).getByText("Released").nextSibling).toHaveTextContent("2026-11-05");
      expect(within(facts).getByText("License").nextSibling).toHaveTextContent("MIT");
      expect(within(facts).getByText("Used by").nextSibling).toHaveTextContent("1 in catalog");
      expect(within(facts).getByText("Dependencies").nextSibling).toHaveTextContent("4 + 2 optional");
      expect(within(facts).getByText("Versions").nextSibling).toHaveTextContent("211");

      expect(screen.getByRole("link", { name: "Repository" })).toHaveAttribute("href", "https://github.com/pydantic/pydantic");
      expect(screen.getByRole("link", { name: "Homepage" })).toHaveAttribute("href", "https://docs.pydantic.dev");
      expect(screen.queryByRole("link", { name: "Documentation" })).not.toBeInTheDocument();
    });

    it("omits the optional part of the dependency fact at zero", async () => {
      api.fetchOverview.mockImplementation((e, n, version) =>
        Promise.resolve(overviewOf(version, { dependencies: { direct: 4, optional: 0, dev: 0, indirect: 0, not_in_catalog: 0 } }))
      );
      renderCard();
      await ready();
      expect(within(document.querySelector(".fact-strip")).getByText("Dependencies").nextSibling).toHaveTextContent(/^4$/);
    });

    it("hides the description row when the package has none, on any ecosystem", async () => {
      api.fetchPackage.mockResolvedValue({ ...PYDANTIC, description: null });
      renderCard();
      await screen.findByLabelText("Version");
      expect(screen.queryByText("No description")).not.toBeInTheDocument();
      expect(document.querySelector(".package-description")).toBeNull();
    });

    it("shows a Go module with no description or license without empty blocks", async () => {
      const MODULE = "github.com/BurntSushi/toml";
      api.fetchPackage.mockResolvedValue({
        ecosystem: "go", name: MODULE, normalized_name: MODULE, namespace: "", purl: `pkg:golang/${MODULE}`, description: null,
        homepage_url: null, repository_url: null, documentation_url: null, license_raw: null, license_spdx: null,
        latest_version: "v1.4.0", sources: [],
      });
      api.fetchVersions.mockResolvedValue({ data: [{ version: "v1.4.0", status: "active", release_date: "2026-01-01T00:00:00Z" }], cursor: null, has_more: false });
      api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version, { lines: [], licenses: [] })));
      renderCard(`/package/go/${MODULE}`);
      await screen.findByRole("tab", { name: /Versions/ });

      expect(document.querySelector(".package-description")).toBeNull();
      expect(within(document.querySelector(".fact-strip")).getByText("License").nextSibling).toHaveTextContent("Not published by the source");
      expect(screen.queryByRole("link", { name: "Repository" })).not.toBeInTheDocument();
      expect(screen.getByText("go get github.com/BurntSushi/toml@v1.4.0")).toBeInTheDocument();
    });

    it("shows Maven groupId and artifactId and RPM distro and name in the header", async () => {
      api.fetchPackage.mockResolvedValue({
        ...PYDANTIC, ecosystem: "maven", name: "org.springframework:spring-core", normalized_name: "spring-core",
        namespace: "org.springframework", purl: "pkg:maven/org.springframework/spring-core", latest_version: "6.1.14",
      });
      api.fetchVersions.mockResolvedValue({ data: [{ version: "6.1.14", status: "active", release_date: null }], cursor: null, has_more: false });
      renderCard("/package/maven/org.springframework:spring-core?tab=sources");
      await screen.findByText("org.springframework", { selector: "code" });
      expect(screen.getByText("spring-core", { selector: "code" })).toBeInTheDocument();
      expect(screen.getByText("org.springframework:spring-core:6.1.14")).toBeInTheDocument();
    });
  });

  describe("banners", () => {
    it("says the version is not the latest and links to the latest", async () => {
      const user = userEvent.setup();
      renderCard("/package/pydantic?version=2.13.5&python=3.11");
      await ready();

      expect(screen.getByText("Not the latest version")).toBeInTheDocument();
      expect(screen.queryByText("latest", { selector: ".tag" })).not.toBeInTheDocument();
      const link = screen.getByRole("link", { name: /go to 2\.14\.3/ });
      expect(link).toHaveAttribute("href", "?version=2.14.3&python=3.11");

      await user.click(link);
      await waitFor(() => expect(screen.getByLabelText("Version")).toHaveValue("2.14.3"));
      expect(screen.queryByText("Not the latest version")).not.toBeInTheDocument();
    });

    it("shows the status of a yanked version and a deprecated package", async () => {
      api.fetchPackage.mockResolvedValue({ ...PYDANTIC, deprecation: { message: "Use pydantic-v3" } });
      renderCard("/package/pydantic?version=2.0.0");
      await ready();

      const banners = screen.getAllByRole("alert").concat(screen.getAllByRole("status"));
      expect(banners.some((node) => node.textContent.includes("Deprecated: Use pydantic-v3"))).toBe(true);
      expect(banners.some((node) => node.textContent.includes("Version 2.0.0") && node.textContent.includes("yanked"))).toBe(true);
    });

    it("shows no status banner for an active version", async () => {
      renderCard();
      await ready();
      expect(screen.queryByText("Version 2.14.3")).not.toBeInTheDocument();
    });

    it("says an unknown version is not in the catalog, keeps the header and links to the latest", async () => {
      api.fetchVersion.mockRejectedValue({ code: "http_error", message: "HTTP 404", status: 404 });
      api.fetchDependencies.mockRejectedValue({ code: "http_error", message: "HTTP 404", status: 404 });
      api.fetchOverview.mockRejectedValue({ code: "http_error", message: "HTTP 404", status: 404 });
      renderCard("/package/pydantic?version=9.9.9");

      expect(await screen.findByText("Version 9.9.9 is not in the catalog")).toBeInTheDocument();
      expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("pydantic");
      expect(screen.getByLabelText("Version")).toHaveValue("9.9.9");
      expect(screen.queryByRole("tab")).not.toBeInTheDocument();
      expect(screen.queryByText("HTTP 404")).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Open 2\.14\.3, the latest/ })).toHaveAttribute("href", "?version=2.14.3");

      api.fetchVersion.mockImplementation((e, n, version) => Promise.resolve({ name: n, version, status: "active", dependencies_state: "hydrated", sources: [] }));
      api.fetchDependencies.mockResolvedValue({ package: "pydantic", version: "2.14.3", depth: 0, dependencies: [] });
      api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version)));
      await userEvent.setup().click(screen.getByRole("link", { name: /Open 2\.14\.3, the latest/ }));
      await waitFor(() => expect(screen.queryByText(/is not in the catalog/)).not.toBeInTheDocument());
      expect(await screen.findByRole("tab", { name: /Versions/ })).toBeInTheDocument();
    });

    it("keeps a version outside the first page of versions in the selector", async () => {
      renderCard("/package/pydantic?version=1.0.0");
      await ready();
      expect(screen.getByLabelText("Version")).toHaveValue("1.0.0");
      expect(api.fetchVersion).toHaveBeenCalledWith("pypi", "pydantic", "1.0.0");
    });
  });

  describe("identifiers", () => {
    it("shows purl@version and the install command with copy buttons", async () => {
      const user = userEvent.setup();
      const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
      renderCard();
      await ready();

      expect(screen.getByText("pkg:pypi/pydantic@2.14.3")).toBeInTheDocument();
      expect(screen.getByText("pip install pydantic==2.14.3")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Copy purl" }));
      expect(write).toHaveBeenCalledWith("pkg:pypi/pydantic@2.14.3");
      expect(await screen.findByText("Copied")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Copy install" }));
      expect(write).toHaveBeenLastCalledWith("pip install pydantic==2.14.3");
    });

    it("says Copy failed and selects the text when the clipboard refuses", async () => {
      const user = userEvent.setup();
      vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
      renderCard();
      await ready();

      await user.click(screen.getByRole("button", { name: "Copy install" }));
      expect(await screen.findByText("Copy failed")).toBeInTheDocument();
      expect(window.getSelection().toString()).toBe("pip install pydantic==2.14.3");
    });
  });

  describe("tabs", () => {
    it("shows Overview first, with counts on Dependencies and Versions", async () => {
      renderCard();
      await ready();

      const tabs = screen.getAllByRole("tab");
      expect(tabs.map((tab) => tab.textContent)).toEqual(["Overview", "Dependencies6", "Used by3", "Versions211", "Compare", "Sources"]);
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-controls", screen.getByRole("tabpanel").id);
      expect(query().tab).toBeUndefined();
    });

    it("keeps ?version= and the environment when the tab changes, and does not reload the data", async () => {
      const user = userEvent.setup();
      renderCard("/package/pydantic?version=2.13.5&python=3.11&os=windows");
      await ready();
      await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(1));
      const calls = () => [api.fetchPackage, api.fetchVersions, api.fetchVersion, api.fetchDependencies, api.fetchOverview, api.fetchVulnerabilities].map((fn) => fn.mock.calls.length);
      const before = calls();

      await user.click(screen.getByRole("tab", { name: /Dependencies/ }));
      expect(query()).toEqual({ version: "2.13.5", python: "3.11", os: "windows", tab: "dependencies" });
      await user.click(screen.getByRole("tab", { name: /Versions/ }));
      expect(query()).toEqual({ version: "2.13.5", python: "3.11", os: "windows", tab: "versions" });
      await user.click(screen.getByRole("tab", { name: "Overview" }));
      expect(query()).toEqual({ version: "2.13.5", python: "3.11", os: "windows" });
      expect(calls()).toEqual(before);
    });

    it("restores the tab from ?tab= and falls back to Overview for an unknown one", async () => {
      renderCard("/package/pydantic?tab=sources&version=2.13.5");
      await screen.findByRole("tab", { name: "Sources" });
      expect(screen.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
      expect(await screen.findByText("Sources of 2.13.5")).toBeInTheDocument();
    });

    it("shows Overview for an unknown ?tab= and drops it from the address", async () => {
      renderCard("/package/pydantic?tab=bogus&version=2.13.5");
      await ready();
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
      await waitFor(() => expect(query()).toEqual({ version: "2.13.5" }));
    });

    it("moves between tabs with the arrow keys, Home and End", async () => {
      const user = userEvent.setup();
      renderCard();
      await ready();

      screen.getByRole("tab", { name: "Overview" }).focus();
      await user.keyboard("{ArrowRight}");
      expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveFocus();
      await user.keyboard("{End}");
      expect(screen.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
      await user.keyboard("{ArrowRight}");
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
      await user.keyboard("{ArrowLeft}");
      expect(screen.getByRole("tab", { name: "Sources" })).toHaveFocus();
      await user.keyboard("{Home}");
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    });

    it("opens a tab from a fact link", async () => {
      const user = userEvent.setup();
      renderCard("/package/pydantic?version=2.13.5");
      await ready();

      const link = within(document.querySelector(".fact-strip")).getByRole("link", { name: "4 + 2 optional" });
      expect(link).toHaveAttribute("href", "?version=2.13.5&tab=dependencies");
      await user.click(link);
      expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveAttribute("aria-selected", "true");
      expect(await screen.findByText("pydantic-core")).toBeInTheDocument();

      await user.click(within(document.querySelector(".fact-strip")).getByRole("link", { name: "211" }));
      expect(screen.getByRole("tab", { name: /Versions/ })).toHaveAttribute("aria-selected", "true");
      expect(query().tab).toBe("versions");
    });

    it("scopes the page to the version chosen in the header", async () => {
      const user = userEvent.setup();
      renderCard();
      await ready();
      await user.selectOptions(screen.getByLabelText("Version"), "2.13.5");

      await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledWith("pypi", "pydantic", "2.13.5"));
      expect(api.fetchVersion).toHaveBeenCalledWith("pypi", "pydantic", "2.13.5");
      expect(query().version).toBe("2.13.5");
      expect(await screen.findByText("Not the latest version")).toBeInTheDocument();
    });
  });

  describe("overview", () => {
    it("shows the blocks with their numbers", async () => {
      renderCard();
      await ready();
      const security = screen.getByRole("heading", { name: "Security" }).parentElement;
      expect(within(security).getByText("No advisories found")).toBeInTheDocument();

      const licenses = screen.getByRole("heading", { name: "Licenses" }).parentElement;
      expect(licenses).toHaveTextContent("This package: MIT");
      expect(licenses).toHaveTextContent("Dependencies: MIT 3 · PSF-2.0 1");

      const dependencies = screen.getByRole("heading", { name: "Dependencies" }).parentElement;
      expect(dependencies).toHaveTextContent("Direct4");
      expect(dependencies).toHaveTextContent("Indirect3");
      expect(dependencies).toHaveTextContent("Plus 2 optional");
      expect(dependencies).toHaveTextContent("1 not in catalog");
      expect(dependencies).toHaveTextContent("in this catalog");
      const [direct, indirect] = dependencies.querySelectorAll(".bar-fill");
      expect(direct.style.width).toBe("100%");
      expect(indirect.style.width).toBe("75%");

      const usedBy = screen.getByRole("heading", { name: "Used by, in this catalog" }).parentElement;
      expect(usedBy).toHaveTextContent("Direct1");
      expect(usedBy).toHaveTextContent("Indirect2");

      const lines = screen.getByRole("heading", { name: "Release lines" }).parentElement;
      expect(within(lines).getAllByRole("listitem")).toHaveLength(3);
      expect(lines).toHaveTextContent("2.14 2.14.3 2026-11-05");
      expect(lines).not.toHaveTextContent("1.10");
    });

    it("draws 12 activity bars, each with its value in a label, and the total", async () => {
      renderCard();
      await ready();
      const chart = screen.getByRole("list", { name: "Releases per month" });
      expect(within(chart).getAllByRole("listitem")).toHaveLength(12);
      expect(within(chart).getByLabelText("Nov 2026: 19 releases")).toBeInTheDocument();
      expect(within(chart).getByLabelText("Jan 2026: 0 releases")).toBeInTheDocument();
      expect(screen.getByText("19 releases in 12 months, prereleases included")).toBeInTheDocument();
    });

    it("says nothing in this catalog depends on a package with zero dependents", async () => {
      api.fetchOverview.mockImplementation((e, n, version) =>
        Promise.resolve(overviewOf(version, { used_by: { direct: 0, indirect: 0 } }))
      );
      renderCard();
      await ready();
      expect(screen.getByText("No packages in this catalog depend on it")).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Used by, in this catalog" })).toBeInTheDocument();
    });

    it("says there are no dependencies, and hides release lines when there are none", async () => {
      api.fetchOverview.mockImplementation((e, n, version) =>
        Promise.resolve(overviewOf(version, {
          dependencies: { direct: 0, optional: 0, dev: 0, indirect: 0, not_in_catalog: 0 }, licenses: [], lines: [],
        }))
      );
      renderCard();
      await ready();
      expect(screen.getByRole("heading", { name: "Licenses" }).parentElement).toHaveTextContent("No dependencies");
      expect(screen.queryByRole("heading", { name: "Release lines" })).not.toBeInTheDocument();
    });

    it("hides release lines for RPM even when the API sent some", async () => {
      api.fetchPackage.mockResolvedValue({ ...PYDANTIC, ecosystem: "rpm", name: "openssl", normalized_name: "openssl", namespace: "rocky-9", purl: "pkg:rpm/rocky/openssl?distro=rocky-9", latest_version: "3.0.7" });
      api.fetchVersions.mockResolvedValue({ data: [{ version: "3.0.7", status: "active", release_date: null }], cursor: null, has_more: false });
      api.fetchArtifacts.mockResolvedValue({ data: [] });
      renderCard("/package/rpm/rocky-9/openssl");
      await screen.findByRole("tab", { name: /Versions/ });
      expect(screen.queryByRole("heading", { name: "Release lines" })).not.toBeInTheDocument();
    });

    it("keeps Security and says so when the overview cannot be loaded", async () => {
      api.fetchOverview.mockRejectedValue({ code: "http_error", message: "HTTP 500", status: 500 });
      renderCard();
      expect(await screen.findByText("Overview is not available")).toBeInTheDocument();
      expect(screen.getByText("No advisories found")).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Versions" })).toBeInTheDocument();
    });

    it("says dependencies are unknown when the overview has no counts", async () => {
      api.fetchOverview.mockImplementation((e, n, version) =>
        Promise.resolve(overviewOf(version, {
          dependencies: { direct: null, optional: null, dev: null, indirect: null, not_in_catalog: null }, licenses: [],
        }))
      );
      renderCard();
      await ready().catch(() => {});
      expect(await screen.findAllByText(/Dependencies unknown/)).not.toHaveLength(0);
      expect(within(document.querySelector(".fact-strip")).getByText("Dependencies").nextSibling).toHaveTextContent("unknown");
      expect(screen.getByRole("tab", { name: "Dependencies" })).toBeInTheDocument();
    });

    it("waits for the dependency tree before asking for the overview of a version still pending", async () => {
      api.fetchVersion.mockImplementation((e, n, version) => Promise.resolve({ name: n, version, status: "active", dependencies_state: "pending", sources: [] }));
      let release;
      api.fetchDependencies.mockReturnValue(new Promise((resolve) => { release = resolve; }));
      renderCard();
      await screen.findByLabelText("Version");
      await waitFor(() => expect(api.fetchDependencies).toHaveBeenCalled());
      await waitFor(() => expect(api.fetchVersion).toHaveBeenCalled());
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(api.fetchOverview).not.toHaveBeenCalled();
      release({ package: "pydantic", version: "2.14.3", depth: 0, dependencies: [] });
      await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledWith("pypi", "pydantic", "2.14.3"));
    });

    it("opens the Dependencies and Versions tabs from the blocks", async () => {
      const user = userEvent.setup();
      renderCard();
      await ready();
      await user.click(screen.getByRole("link", { name: "Open dependencies" }));
      expect(screen.getByRole("tab", { name: /Dependencies/ })).toHaveAttribute("aria-selected", "true");
    });
  });
});
