import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";

import App from "../../App";
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

function node(name, patch = {}) {
  return {
    name,
    constraint: "*",
    dependency_type: "runtime",
    extra_name: null,
    environment_markers: null,
    resolved_version: "1.0.0",
    resolved_name: null,
    license: "MIT",
    cycle: false,
    children: [],
    ...patch,
  };
}

// root -> a (PSF-2.0) -> b (no license) -> c;  root -> d;  a -> ghost (not in catalog); c -> a (a cycle)
const TREE = [
  node("a", {
    constraint: ">=1",
    license: "PSF-2.0",
    children: [
      node("b", { license: null, children: [node("c", { children: [node("a", { cycle: true, license: "PSF-2.0" })] })] }),
      node("ghost", { resolved_version: null, license: null }),
    ],
  }),
  node("d", { dependency_type: "optional", resolved_version: "2.0.0" }),
];

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.search}</output>;
}

function mockPackage(ecosystem, name, nodes, extra = {}) {
  const [namespace, normalized] = name.includes(":") ? name.split(":") : ["", name];
  api.fetchPackage.mockResolvedValue({
    ecosystem,
    name: normalized,
    normalized_name: normalized,
    namespace,
    description: "A package",
    license_spdx: "MIT",
    latest_version: "1.0.0",
    sources: [],
  });
  api.fetchDependencies.mockResolvedValue({ package: name, version: "1.0.0", depth: 3, dependencies: nodes, ...extra });
}

async function open(path, ecosystem = "pypi", name = "root", nodes = TREE, extra = {}) {
  mockPackage(ecosystem, name, nodes, extra);
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
      <Where />
    </MemoryRouter>
  );
  await screen.findByText("A package");
}

// The address without `version`, which the page adds once the latest version is known.
const where = () => {
  const query = new URLSearchParams(screen.getByTestId("where").textContent);
  query.delete("version");
  return `?${query.toString()}`;
};
const bodyRows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);
const cells = (row) => within(row).getAllByRole("cell").map((cell) => cell.textContent);

describe("Dependencies tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchOverview.mockImplementation((ecosystem, name, version) =>
      Promise.resolve({
        version,
        dependencies: { direct: 2, optional: 0, dev: 0, indirect: 0, not_in_catalog: 0 },
        used_by: { direct: 0, indirect: 0 },
        licenses: [],
        versions: { total: 1, prereleases: 0, not_active: 0 },
        activity: [],
        lines: [],
      })
    );
    api.fetchVersions.mockResolvedValue({
      data: [{ version: "1.0.0", status: "active", release_date: "2026-01-01T00:00:00Z" }],
      cursor: null,
      has_more: false,
    });
    api.fetchVulnerabilities.mockResolvedValue({ package: "root", coverage: "collected", data: [] });
    api.fetchVersion.mockResolvedValue({ name: "root", version: "1.0.0", status: "active", dependencies_state: "hydrated", sources: [] });
    api.fetchArtifacts.mockResolvedValue({ data: [] });
    api.fetchDependents.mockResolvedValue({ data: [], cursor: null, has_more: false, totals: { direct: 0, indirect: 0 } });
  });

  it("opens on the table, not the tree", async () => {
    await open("/package/pypi/root?tab=dependencies");
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(document.querySelector(".dep-tree")).toBeNull();
    expect(screen.queryByRole("button", { name: "Collapse tree" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh tree" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Table" })).toHaveAttribute("aria-current", "true");
    expect(within(screen.getByRole("table")).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Package",
      "Requires",
      "Resolved",
      "Type",
      "Relation",
      "License",
    ]);
    expect(screen.getByLabelText("Python version")).toBeInTheDocument();
    expect(where()).toBe("?tab=dependencies");
  });

  it("restores the tree from ?deps=tree and moves between the views, leaving Table out of the address", async () => {
    const user = userEvent.setup();
    await open("/package/pypi/root?tab=dependencies&deps=tree");
    expect(await screen.findByRole("button", { name: "Collapse tree" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh tree" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Python version")).toBeInTheDocument();
    expect(where()).toBe("?tab=dependencies&deps=tree");

    await user.click(screen.getByRole("link", { name: "Table" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(where()).toBe("?tab=dependencies");

    await user.click(screen.getByRole("link", { name: "Tree" }));
    expect(await screen.findByRole("button", { name: "Collapse tree" })).toBeInTheDocument();
    expect(where()).toBe("?tab=dependencies&deps=tree");
    expect(api.fetchDependencies).toHaveBeenCalledTimes(1);
  });

  it("summarises the tree: relations, rows not in catalog, licenses, and the scope of the numbers", async () => {
    await open("/package/pypi/root?tab=dependencies");
    expect(await screen.findByText("2 direct · 3 indirect · 1 not resolved")).toBeInTheDocument();
    expect(screen.getByText("Licenses: MIT 2 · PSF-2.0 1 · unknown 1")).toBeInTheDocument();
    expect(screen.getByText("For this environment. Overview counts latest versions across the catalog.")).toBeInTheDocument();
  });

  it("lists direct rows first, then by name, with license, via path and the not-in-catalog mark", async () => {
    await open("/package/pypi/root?tab=dependencies");
    await screen.findByRole("table");
    const rows = bodyRows();
    expect(rows.map((row) => cells(row)[0])).toEqual(["a", "d", "bvia a", "cvia a → b", "ghostvia a"]);
    expect(cells(rows[0])).toEqual(["a", ">=1", "1.0.0", "runtime", "Direct", "PSF-2.0"]);
    expect(cells(rows[1])).toEqual(["d", "—", "2.0.0", "optional", "Direct", "MIT"]);
    expect(cells(rows[2])).toEqual(["bvia a", "—", "1.0.0", "runtime", "Indirect", "unknown"]);
    expect(within(rows[3]).getByText("via a → b")).toHaveClass("muted");
    expect(cells(rows[4])).toEqual(["ghostvia a", "—", "not resolved", "runtime", "Indirect", "unknown"]);
    expect(within(rows[0]).queryByText(/^via/)).not.toBeInTheDocument();
  });

  it("filters by relation from the chips, in ?dep_relation=, and by name without touching the address", async () => {
    const user = userEvent.setup();
    await open("/package/pypi/root?tab=dependencies");
    await screen.findByRole("table");
    expect(screen.getByRole("link", { name: "All 5" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "Direct 2" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Indirect 3" }));
    expect(bodyRows()).toHaveLength(3);
    expect(where()).toBe("?tab=dependencies&dep_relation=indirect");

    await user.type(screen.getByLabelText("Filter by name"), "GH");
    expect(bodyRows()).toHaveLength(1);
    expect(cells(bodyRows()[0])[0]).toBe("ghostvia a");
    expect(where()).toBe("?tab=dependencies&dep_relation=indirect");

    await user.click(screen.getByRole("link", { name: "All 5" }));
    expect(bodyRows()).toHaveLength(1);
    expect(where()).toBe("?tab=dependencies");

    await user.clear(screen.getByLabelText("Filter by name"));
    await user.type(screen.getByLabelText("Filter by name"), "nothing");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("No dependencies match")).toBeInTheDocument();
  });

  it("reads ?dep_relation= and leaves ?relation= to Used by", async () => {
    await open("/package/pypi/root?tab=dependencies&dep_relation=direct");
    await screen.findByRole("table");
    expect(bodyRows()).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Direct 2" })).toHaveAttribute("aria-current", "true");
  });

  it("ignores ?relation=, which belongs to Used by", async () => {
    await open("/package/pypi/root?tab=dependencies&relation=direct");
    await screen.findByRole("table");
    expect(bodyRows()).toHaveLength(5);
    expect(screen.getByRole("link", { name: "All 5" })).toHaveAttribute("aria-current", "true");
  });

  it("refetches for another environment and the table follows", async () => {
    const user = userEvent.setup();
    await open("/package/pypi/root?tab=dependencies");
    await screen.findByRole("table");
    expect(bodyRows()).toHaveLength(5);

    api.fetchDependencies.mockResolvedValue({ package: "root", version: "1.0.0", depth: 1, dependencies: [node("only-on-311")] });
    await user.selectOptions(screen.getByLabelText("Python version"), "3.11");
    await waitFor(() => expect(bodyRows().map((row) => cells(row)[0])).toEqual(["only-on-311"]));
    expect(api.fetchDependencies).toHaveBeenLastCalledWith("pypi", "root", "1.0.0", expect.objectContaining({ python_version: "3.11" }));
  });

  it("says when the source has no dependency data", async () => {
    await open("/package/pypi/root?tab=dependencies", "pypi", "root", [], { dependencies_unknown: true });
    expect(await screen.findByText("The source has no dependency data for this version")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("No dependencies for this environment.")).not.toBeInTheDocument();
  });

  it("says No dependencies for this environment when the tree is empty", async () => {
    await open("/package/pypi/root?tab=dependencies", "pypi", "root", []);
    expect(await screen.findByText("No dependencies for this environment.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  describe("links", () => {
    async function linkOf(path, ecosystem, name, nodes, text) {
      await open(path, ecosystem, name, nodes);
      await screen.findByRole("table");
      return screen.getByRole("link", { name: text });
    }

    it("links a PyPI package at the resolved version, and a package without a catalog version not at all", async () => {
      const link = await linkOf("/package/pypi/root?tab=dependencies", "pypi", "root", [node("werkzeug", { resolved_version: "3.1.0" }), node("ghost", { resolved_version: null })], "werkzeug");
      expect(link).toHaveAttribute("href", "/package/pypi/werkzeug?version=3.1.0");
      expect(screen.queryByRole("link", { name: "ghost" })).not.toBeInTheDocument();
      expect(screen.getByText("ghost")).toBeInTheDocument();
    });

    it("links a scoped npm package", async () => {
      const link = await linkOf("/package/npm/@angular/core?tab=dependencies", "npm", "@angular/core", [node("@angular/common", { resolved_version: "19.0.0" })], "@angular/common");
      expect(link).toHaveAttribute("href", "/package/npm/@angular/common?version=19.0.0");
    });

    it("links a Maven groupId:artifactId", async () => {
      const link = await linkOf(
        "/package/maven/org.example:app?tab=dependencies",
        "maven",
        "org.example:app",
        [node("org.slf4j:slf4j-api", { resolved_version: "2.0.9" })],
        "org.slf4j:slf4j-api"
      );
      expect(link).toHaveAttribute("href", "/package/maven/org.slf4j:slf4j-api?version=2.0.9");
    });

    it("links an RPM capability to the package that provides it, at the tree's arch", async () => {
      const nodes = [node("libssl.so.3()(64bit)", { resolved_name: "rocky-9/openssl-libs", resolved_version: "1:3.0.7-27.el9", constraint: "*" })];
      await open("/package/rpm/rocky-9/app?tab=dependencies&arch=aarch64", "rpm", "rocky-9:app", nodes);
      await screen.findByRole("table");
      const link = screen.getByRole("link", { name: "rocky-9/openssl-libs" });
      expect(link).toHaveAttribute("href", "/package/rpm/rocky-9/openssl-libs?version=1%3A3.0.7-27.el9&arch=aarch64");
      expect(screen.getByText("provides libssl.so.3()(64bit)")).toBeInTheDocument();
    });
  });
});
