// The Compare tab of the package card (SPEC-CATALOG-PAGES.md §5).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import PackagePage from "../PackagePage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchArtifacts: vi.fn(),
  fetchDependencies: vi.fn(),
  fetchCompare: vi.fn(),
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
  { version: "2.12.0", status: "yanked", release_date: "2026-06-01T09:00:00Z" },
  { version: "2.0.0", status: "active", release_date: "2024-01-01T09:00:00Z" },
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

const side = (version, patch = {}) => ({ version, release_date: VERSIONS.find((row) => row.version === version)?.release_date, status: "active", ...patch });
const NONE = { added: [], removed: [], changed: [], unchanged: 0 };
const DIFF = {
  from: side("2.13.5"),
  to: side("2.14.3"),
  dependencies: {
    added: [{ name: "annotated-types", constraint: ">=0.7", dependency_type: "runtime" }],
    removed: [{ name: "old-helper", constraint: "<2", dependency_type: "dev" }],
    changed: [{ name: "pydantic-core", dependency_type: "runtime", from: "==2.46.5", to: "==2.50.4" }],
    unchanged: 1,
  },
  dependencies_unknown: false,
};

describe("compare tab", () => {
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
        sources: [],
      })
    );
    api.fetchDependencies.mockResolvedValue({ package: "pydantic", version: "2.14.3", depth: 1, dependencies: [] });
    api.fetchOverview.mockImplementation((ecosystem, name, version) => Promise.resolve(overviewOf(version)));
    api.fetchCompare.mockResolvedValue(DIFF);
  });

  async function open(path = "/package/pydantic?tab=compare") {
    renderCard(path);
    await screen.findByRole("tab", { name: "Compare" });
    await screen.findByLabelText("From");
  }

  it("is the tab before Sources and asks for the API's default from", async () => {
    await open();
    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent.replace(/\d+$/, ""));
    expect(tabs.indexOf("Compare")).toBe(tabs.indexOf("Sources") - 1);
    await screen.findByText("Compared with the previous stable version, 2.13.5");
    expect(api.fetchCompare).toHaveBeenCalledWith("pypi", "pydantic", { from: "", to: "2.14.3" });
    expect(screen.getByLabelText("From")).toHaveValue("2.13.5");
    expect(query().from).toBeUndefined();
    expect(screen.getByText("change it above")).toBeInTheDocument();
  });

  it("restores ?from= and writes it when the select changes", async () => {
    const user = userEvent.setup();
    await open("/package/pydantic?version=2.14.3&tab=compare&from=2.0.0");
    expect(api.fetchCompare).toHaveBeenCalledWith("pypi", "pydantic", { from: "2.0.0", to: "2.14.3" });
    await waitFor(() => expect(screen.getByLabelText("From")).toHaveValue("2.0.0"));
    expect(query()).toMatchObject({ tab: "compare", from: "2.0.0" });

    await user.selectOptions(screen.getByLabelText("From"), "2.12.0");
    await waitFor(() => expect(api.fetchCompare).toHaveBeenLastCalledWith("pypi", "pydantic", { from: "2.12.0", to: "2.14.3" }));
    expect(query().from).toBe("2.12.0");
    const options = within(screen.getByLabelText("From")).getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["2.13.5", "2.12.0", "2.0.0"]);   // only lower versions
  });

  it("keeps a from outside the loaded versions among the options", async () => {
    await open("/package/pydantic?tab=compare&from=1.0.0");
    const options = within(screen.getByLabelText("From")).getAllByRole("option").map((option) => option.textContent);
    expect(options[0]).toBe("1.0.0");
  });

  it("offers the newest 200 versions in the select, not only the header's first screen", async () => {
    const long = Array.from({ length: 120 }, (_, index) => ({
      version: `1.${119 - index}.0`, status: "active", release_date: "2020-01-01T00:00:00Z",
    }));
    api.fetchVersions.mockImplementation((e, n, params) =>
      Promise.resolve({ data: params.limit === 200 ? [...VERSIONS, ...long] : VERSIONS, cursor: null, has_more: false })
    );
    await open();
    await waitFor(() => expect(within(screen.getByLabelText("From")).getAllByRole("option").length).toBeGreaterThan(100));
    expect(api.fetchVersions).toHaveBeenCalledWith("pypi", "pydantic", { limit: 200 });
    const options = within(screen.getByLabelText("From")).getAllByRole("option").map((option) => option.textContent);
    expect(options).toContain("1.0.0");
    expect(options).not.toContain("2.14.3");   // the compared version is not its own base
  });

  it("keeps the header's versions in the select when the longer list cannot be loaded", async () => {
    api.fetchVersions.mockImplementation((e, n, params) =>
      params.limit === 200 ? Promise.reject({ message: "HTTP 500", status: 500 }) : Promise.resolve({ data: VERSIONS, cursor: null, has_more: false })
    );
    await open();
    expect(within(screen.getByLabelText("From")).getAllByRole("option").map((option) => option.textContent)).toEqual(["2.13.5", "2.12.0", "2.0.0"]);
  });

  it("shows scoped dependencies under their own names, one row each", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    api.fetchCompare.mockResolvedValue({
      ...DIFF,
      dependencies: {
        ...NONE,
        changed: [
          { name: "core", dependency_type: "runtime", from: "^1", to: "^2" },
          { name: "@babel/core", dependency_type: "runtime", from: "^7", to: "^8" },
          { name: "@other/core", dependency_type: "runtime", from: "^1", to: "^2" },
        ],
      },
    });
    await open();
    const table = await screen.findByRole("table");
    const names = within(table).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent);
    expect(names).toEqual(["core runtime", "@babel/core runtime", "@other/core runtime"]);
    expect(error).not.toHaveBeenCalled();   // no duplicate React keys
    error.mockRestore();
  });

  it("asks again when the header's version changes", async () => {
    const user = userEvent.setup();
    await open("/package/pydantic?tab=compare&from=2.0.0");
    await user.selectOptions(screen.getByLabelText("Version"), "2.13.5");
    await waitFor(() => expect(api.fetchCompare).toHaveBeenLastCalledWith("pypi", "pydantic", { from: "2.0.0", to: "2.13.5" }));
  });

  it("shows changed, removed and added rows, the dates and the notes", async () => {
    await open();
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Dependency", "2.13.5", "2.14.3"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    const [changed, removed, added] = rows;
    expect(within(changed).getByText("pydantic-core")).toBeInTheDocument();
    expect(within(changed).getByText("runtime")).toHaveClass("tag");
    const changedCells = within(changed).getAllByRole("cell");
    expect(changedCells[1]).toHaveTextContent("==2.46.5");
    expect(changedCells[2]).toHaveTextContent("==2.50.4");
    expect(changedCells[1]).toHaveClass("diff-old");
    expect(changedCells[2]).toHaveClass("diff-new");
    const removedCells = within(removed).getAllByRole("cell");
    expect(removedCells[1]).toHaveTextContent("removed");
    expect(removedCells[1]).toHaveTextContent("<2");
    expect(removedCells[2]).toBeEmptyDOMElement();
    const addedCells = within(added).getAllByRole("cell");
    expect(addedCells[1]).toBeEmptyDOMElement();
    expect(addedCells[2]).toHaveTextContent("added");
    expect(addedCells[2]).toHaveTextContent(">=0.7");
    expect(screen.getByText("1 dependency unchanged")).toBeInTheDocument();
    expect(screen.getByText("License per version is not stored")).toBeInTheDocument();
    expect(screen.getByText(/released 2026-09-01/)).toHaveTextContent("released 2026-09-01 → 2026-11-05");
  });

  it("tags a side that is not active in the dates line", async () => {
    api.fetchCompare.mockResolvedValue({ ...DIFF, from: side("2.13.5", { status: "yanked" }) });
    await open();
    expect(await screen.findByText(/released 2026-09-01/)).toHaveTextContent("released 2026-09-01 yanked → 2026-11-05");
  });

  it("says so when nothing changed", async () => {
    api.fetchCompare.mockResolvedValue({ ...DIFF, dependencies: { ...NONE, unchanged: 4 } });
    await open();
    expect(await screen.findByText("No dependency changes between these versions")).toBeInTheDocument();
    expect(screen.getByText("4 dependencies unchanged")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says so for the earliest stable version", async () => {
    api.fetchCompare.mockResolvedValue({ ...DIFF, from: null, dependencies: NONE });
    await open();
    expect(await screen.findByText("This is the earliest stable version")).toBeInTheDocument();
    expect(screen.getByText("License per version is not stored")).toBeInTheDocument();
    expect(screen.queryByText(/unchanged/)).not.toBeInTheDocument();
  });

  it("says so when the source has no dependency data", async () => {
    api.fetchCompare.mockResolvedValue({ ...DIFF, dependencies: NONE, dependencies_unknown: true });
    await open();
    expect(await screen.findByText("The source has no dependency data for one of these versions")).toBeInTheDocument();
    expect(screen.queryByText("No dependency changes between these versions")).not.toBeInTheDocument();
  });

  it("keeps the newest answer when an older one arrives later", async () => {
    const user = userEvent.setup();
    let answerFirst;
    api.fetchCompare.mockImplementationOnce(() => new Promise((resolve) => (answerFirst = resolve)));
    await open();
    api.fetchCompare.mockResolvedValueOnce({
      ...DIFF,
      from: side("2.12.0"),
      dependencies: { ...NONE, changed: [{ name: "newer-answer", dependency_type: "runtime", from: "1", to: "2" }] },
    });
    await user.selectOptions(screen.getByLabelText("From"), "2.12.0");
    expect(await screen.findByText("newer-answer")).toBeInTheDocument();
    answerFirst(DIFF);
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("newer-answer")).toBeInTheDocument();
    expect(screen.queryByText("pydantic-core")).not.toBeInTheDocument();
  });
});
