// Answers that arrive out of order: a slow answer for another version or package never writes over the current page.
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import PackagePage from "../PackagePage";
import { useDependents } from "../package/useDependents";
import { useVersionList } from "../package/useVersionList";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchArtifacts: vi.fn(),
  fetchCompare: vi.fn(),
  fetchDependencies: vi.fn(),
  fetchDependents: vi.fn(),
  fetchOverview: vi.fn(),
  fetchPackage: vi.fn(),
  fetchVersion: vi.fn(),
  fetchVersions: vi.fn(),
  fetchVulnerabilities: vi.fn(),
}));

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const MONTHS = Array.from({ length: 12 }, (_, index) => `2026-${String(index + 1).padStart(2, "0")}`);

function overviewOf(version, patch = {}) {
  return {
    version,
    dependencies: { direct: 1, optional: 0, dev: 0, indirect: 0, not_in_catalog: 0 },
    used_by: { direct: 1, indirect: 2 },
    licenses: [],
    versions: { total: 3, prereleases: 0, not_active: 0, deprecated: 0 },
    activity: MONTHS.map((month) => ({ month, releases: 0 })),
    lines: [],
    ...patch,
  };
}

const pkgOf = (name, description, latest = "2.14.3") => ({
  ecosystem: "npm", name, normalized_name: name, namespace: "", purl: `pkg:npm/${name}`, description,
  license_raw: "MIT", license_spdx: "MIT", latest_version: latest, sources: [],
});

const VERSIONS = [
  { version: "2.14.3", status: "active", release_date: "2026-11-05T09:00:00Z" },
  { version: "2.13.5", status: "active", release_date: "2026-09-01T09:00:00Z" },
  { version: "2.0.0", status: "active", release_date: "2024-01-01T09:00:00Z" },
];

const node = (name) => ({
  name, constraint: "^1", dependency_type: "runtime", extra_name: null, environment_markers: null, resolved_version: "1.0.0", cycle: false, children: [],
});

function LocationProbe() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderCard(path) {
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <LocationProbe />
      <Link to="/package/npm/beta">to beta</Link>
      <Routes>
        <Route path="/package/:ecosystem/*" element={<PackagePage />} />
      </Routes>
    </MemoryRouter>
  );
}

const query = () => Object.fromEntries(new URLSearchParams(screen.getByTestId("search").textContent));

describe("answers out of order", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchPackage.mockImplementation((e, name) => Promise.resolve(pkgOf(name, `The package ${name}`)));
    api.fetchVersions.mockResolvedValue({ data: VERSIONS, cursor: null, has_more: false });
    api.fetchVulnerabilities.mockResolvedValue({ package: "alpha", coverage: "collected", data: [] });
    api.fetchVersion.mockImplementation((e, name, version) =>
      Promise.resolve({
        name, version, purl: `pkg:npm/${name}@${version}`, release_date: null, status: "active", dependencies_state: "pending", sources: [],
      })
    );
    api.fetchDependencies.mockResolvedValue({ package: "alpha", version: "2.14.3", depth: 1, dependencies: [node("fresh-dep")] });
    api.fetchOverview.mockImplementation((e, n, version) => Promise.resolve(overviewOf(version)));
  });

  it("drops the tree of a slow version and does not strand the overview of the next one", async () => {
    const user = userEvent.setup();
    const slow = deferred();
    api.fetchDependencies.mockImplementation((e, name, version) =>
      version === "2.13.5" ? slow.promise : Promise.resolve({ package: name, version, depth: 1, dependencies: [node("dep-of-" + version.replaceAll(".", "-"))] })
    );
    renderCard("/package/npm/alpha");
    await screen.findByText("The package alpha");
    await screen.findByRole("tab", { name: /Versions\s*3/ });

    await user.selectOptions(screen.getByLabelText("Version"), "2.13.5");
    await waitFor(() => expect(api.fetchDependencies).toHaveBeenCalledWith("npm", "alpha", "2.13.5", expect.anything()));
    await user.selectOptions(screen.getByLabelText("Version"), "2.0.0");
    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledWith("npm", "alpha", "2.0.0"));
    await screen.findByRole("tab", { name: /Versions\s*3/ });

    // The slow answer for 2.13.5 arrives after 2.0.0 settled.
    await act(async () => slow.resolve({ package: "alpha", version: "2.13.5", depth: 1, dependencies: [node("dep-of-2-13-5")] }));

    expect(screen.queryByText("Loading overview...")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Dependencies/ }));
    expect(await screen.findByText("dep-of-2-0-0")).toBeInTheDocument();
    expect(screen.queryByText("dep-of-2-13-5")).not.toBeInTheDocument();
    expect(query().version).toBe("2.0.0");
  });

  it("drops the package that answers after the page moved to another one", async () => {
    const user = userEvent.setup();
    const slow = deferred();
    api.fetchPackage.mockImplementation((e, name) =>
      name === "alpha" ? slow.promise : Promise.resolve(pkgOf(name, `The package ${name}`, "1.0.0"))
    );
    api.fetchVersions.mockImplementation((e, name) =>
      Promise.resolve({ data: name === "alpha" ? VERSIONS : [{ version: "1.0.0", status: "active", release_date: null }], cursor: null, has_more: false })
    );
    renderCard("/package/npm/alpha");
    await user.click(screen.getByRole("link", { name: "to beta" }));
    await screen.findByText("The package beta");

    await act(async () => slow.resolve(pkgOf("alpha", "The package alpha")));

    expect(screen.getByText("The package beta")).toBeInTheDocument();
    expect(screen.queryByText("The package alpha")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Version")).toHaveValue("1.0.0");
    expect(query().version).toBe("1.0.0");
  });

  it("does not wait for the tree to load the overview of a hydrated version", async () => {
    api.fetchVersion.mockImplementation((e, name, version) =>
      Promise.resolve({ name, version, purl: "", release_date: null, status: "active", dependencies_state: "hydrated", sources: [] })
    );
    api.fetchDependencies.mockReturnValue(new Promise(() => {}));   // a cold tree that never answers
    renderCard("/package/npm/alpha");
    expect(await screen.findByRole("tab", { name: /Versions\s*3/ })).toBeInTheDocument();
    expect(api.fetchOverview).toHaveBeenCalledTimes(1);
  });

  it("waits for the tree before it loads the overview of a version still pending", async () => {
    const tree = deferred();
    api.fetchDependencies.mockReturnValue(tree.promise);
    renderCard("/package/npm/alpha");
    await screen.findByText("The package alpha");
    await waitFor(() => expect(api.fetchDependencies).toHaveBeenCalled());
    await act(async () => {});
    expect(api.fetchOverview).not.toHaveBeenCalled();

    await act(async () => tree.resolve({ package: "alpha", version: "2.14.3", depth: 0, dependencies: [] }));
    expect(await screen.findByRole("tab", { name: /Versions\s*3/ })).toBeInTheDocument();
    expect(api.fetchOverview).toHaveBeenCalledTimes(1);
  });

  it("holds the comparison back while the tree hydrates the same version, and not for a hydrated one", async () => {
    const tree = deferred();
    api.fetchCompare.mockResolvedValue({
      from: { version: "2.13.5", release_date: null, status: "active" },
      to: { version: "2.14.3", release_date: null, status: "active" },
      dependencies: { added: [], removed: [], changed: [], unchanged: 0 },
      dependencies_unknown: false,
    });
    api.fetchDependencies.mockReturnValue(tree.promise);
    renderCard("/package/npm/alpha?tab=compare");
    await screen.findByText("The package alpha");
    await waitFor(() => expect(api.fetchDependencies).toHaveBeenCalled());
    await act(async () => {});
    expect(api.fetchCompare).not.toHaveBeenCalled();

    await act(async () => tree.resolve({ package: "alpha", version: "2.14.3", depth: 0, dependencies: [] }));
    await waitFor(() => expect(api.fetchCompare).toHaveBeenCalledTimes(1));
  });

  it("keeps the relation filter of Used by apart from the one of Dependencies", async () => {
    const user = userEvent.setup();
    api.fetchDependents.mockResolvedValue({ totals: { direct: 1, indirect: 2 }, data: [], cursor: null, has_more: false });
    api.fetchDependencies.mockResolvedValue({
      package: "alpha", version: "2.14.3", depth: 2,
      dependencies: [{ ...node("direct-dep"), children: [node("deep-dep")] }],
    });
    renderCard("/package/npm/alpha?tab=used-by&relation=direct");
    await screen.findByRole("link", { name: "Direct 1" });
    expect(screen.getByRole("link", { name: "Direct 1" })).toHaveAttribute("aria-current", "true");

    await user.click(screen.getByRole("tab", { name: /Dependencies/ }));
    await screen.findByRole("link", { name: /^Indirect/ });
    expect(screen.getByRole("link", { name: /^All/ })).toHaveAttribute("aria-current", "true");
    expect(query().relation).toBeUndefined();

    await user.click(screen.getByRole("link", { name: /^Indirect/ }));
    await waitFor(() => expect(query().dep_relation).toBe("indirect"));
    expect(query().relation).toBeUndefined();

    await user.click(screen.getByRole("tab", { name: /Used by/ }));
    await waitFor(() => expect(query().relation).toBe("direct"));
    expect(query().dep_relation).toBeUndefined();
  });
});

describe("lists that belong to a package", () => {
  beforeEach(() => vi.clearAllMocks());

  it("useDependents drops the answer for the package the page left", async () => {
    const late = deferred();
    api.fetchDependents.mockImplementation((e, name) =>
      name === "alpha"
        ? late.promise
        : Promise.resolve({ totals: { direct: 1, indirect: 0 }, data: [{ purl: "pkg:npm/beta-user" }], cursor: null, has_more: false })
    );
    const { result, rerender } = renderHook(({ name }) => useDependents("npm", name, true, ""), { initialProps: { name: "alpha" } });
    rerender({ name: "beta" });
    await waitFor(() => expect(result.current.state).toBe("ready"));

    await act(async () => late.resolve({ totals: { direct: 9, indirect: 9 }, data: [{ purl: "pkg:npm/alpha-user" }], cursor: null, has_more: false }));

    expect(result.current.rows.map((row) => row.purl)).toEqual(["pkg:npm/beta-user"]);
    expect(result.current.totals).toEqual({ direct: 1, indirect: 0 });
    expect(api.fetchDependents).toHaveBeenCalledTimes(2);
  });

  it("useVersionList drops the answer for the package the page left", async () => {
    const late = deferred();
    api.fetchVersions.mockImplementation((e, name) =>
      name === "alpha" ? late.promise : Promise.resolve({ data: [{ version: "1.0.0" }], cursor: null, has_more: false })
    );
    const { result, rerender } = renderHook(({ name }) => useVersionList("npm", name, "all"), { initialProps: { name: "alpha" } });
    rerender({ name: "beta" });
    await waitFor(() => expect(result.current.state).toBe("ready"));

    await act(async () => late.resolve({ data: [{ version: "9.9.9" }], cursor: null, has_more: false }));

    expect(result.current.rows.map((row) => row.version)).toEqual(["1.0.0"]);
    expect(api.fetchVersions).toHaveBeenCalledTimes(2);
  });
});
