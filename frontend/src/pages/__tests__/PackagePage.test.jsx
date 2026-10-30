import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import App from "../../App";
import PackagePage from "../PackagePage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchDependencies: vi.fn(),
  fetchPackage: vi.fn(),
  fetchVersion: vi.fn(),
  fetchVersions: vi.fn(),
  fetchVulnerabilities: vi.fn(),
}));

const RAW_BINARY_DESCRIPTION = `binary
======

.. image:: https://img.shields.io/pypi/v/binary.svg

binary provides a **bug-free** and easy way to convert between and within binary and decimal units.

Installation
------------

.. code-block:: bash

   pip install binary
`;

describe("PackagePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchPackage.mockResolvedValue({
      ecosystem: "pypi",
      name: "Flask",
      normalized_name: "flask",
      description: "A lightweight WSGI framework",
      homepage_url: "https://palletsprojects.com/p/flask/",
      repository_url: "https://github.com/pallets/flask",
      license_raw: "BSD-3-Clause",
      license_spdx: "BSD-3-Clause",
      latest_version: "3.1.3",
      synced_at: "2026-03-09T00:00:00Z",
    });
    api.fetchVersions.mockResolvedValue({
      data: [
        { version: "3.1.3", status: "active", requires_python: ">=3.9", release_date: "2026-01-01T00:00:00Z" },
        { version: "3.1.2", status: "active", requires_python: ">=3.9", release_date: "2025-12-01T00:00:00Z" },
      ],
      cursor: null,
      has_more: false,
    });
    api.fetchVulnerabilities.mockResolvedValue({
      package: "flask",
      data: [],
    });
    api.fetchVersion.mockResolvedValue({
      name: "flask",
      version: "3.1.3",
      status: "active",
      dependencies_state: "hydrated",
      sources: [],
    });
    api.fetchDependencies.mockResolvedValue({
      package: "flask",
      version: "3.1.3",
      depth: 1,
      dependencies: [
        {
          name: "werkzeug",
          constraint: ">=3.1.0",
          dependency_type: "runtime",
          extra_name: null,
          environment_markers: null,
          resolved_version: "3.1.0",
          cycle: false,
          children: [],
        },
      ],
    });
  });

  it("renders package details, versions and dependency tree", async () => {
    render(
      <MemoryRouter
        initialEntries={["/package/flask"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText("A lightweight WSGI framework")).toBeInTheDocument();
    expect(screen.getByText((content) => content.includes("BSD-3-Clause"))).toBeInTheDocument();
    expect(await screen.findByText("werkzeug")).toBeInTheDocument();
    expect(screen.getByText(">=3.1.0")).toBeInTheDocument();
    expect(screen.getByText("3.1.0")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();

    await waitFor(() => {
      expect(api.fetchDependencies).toHaveBeenCalledWith("pypi", "flask", "3.1.3", {
        include_optional: false,
        python_version: "3.12",
        os: "linux",
      });
    });
  });

  it("renders a cleaned package description instead of raw rst directives", async () => {
    api.fetchPackage.mockResolvedValueOnce({
      ecosystem: "pypi",
      name: "binary",
      normalized_name: "binary",
      description: RAW_BINARY_DESCRIPTION,
      homepage_url: "https://example.com/binary",
      repository_url: "https://github.com/example/binary",
      license_raw: "MIT",
      license_spdx: "MIT",
      latest_version: "1.0.0",
      synced_at: "2026-03-09T00:00:00Z",
    });
    api.fetchVersions.mockResolvedValueOnce({
      data: [{ version: "1.0.0", status: "active", requires_python: ">=3.9", release_date: "2026-01-01T00:00:00Z" }],
      cursor: null,
      has_more: false,
    });
    api.fetchDependencies.mockResolvedValueOnce({
      package: "binary",
      version: "1.0.0",
      depth: 0,
      dependencies: [],
    });

    render(
      <MemoryRouter
        initialEntries={["/package/binary"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText(/binary provides a bug-free and easy way to convert/i)).toBeInTheDocument();
    expect(screen.getByText("Installation")).toBeInTheDocument();
    expect(screen.queryByText(/\.\. image::/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/\.\. code-block::/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/pip install binary/i)).not.toBeInTheDocument();
  });

  it("reloads dependency tree for selected version", async () => {
    const user = userEvent.setup();
    api.fetchDependencies
      .mockResolvedValueOnce({
        package: "flask",
        version: "3.1.3",
        depth: 0,
        dependencies: [],
      })
      .mockResolvedValueOnce({
        package: "flask",
        version: "3.1.2",
        depth: 0,
        dependencies: [],
      });

    render(
      <MemoryRouter
        initialEntries={["/package/flask"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    await screen.findByText("A lightweight WSGI framework");
    expect(await screen.findByText("No runtime dependencies for this environment.")).toBeInTheDocument();
    const versionSelect = screen.getByLabelText("Version");
    await user.selectOptions(versionSelect, "3.1.2");

    await waitFor(() => {
      expect(api.fetchDependencies).toHaveBeenCalledWith("pypi", "flask", "3.1.2", {
        include_optional: false,
        python_version: "3.12",
        os: "linux",
      });
    });
  });

  it("reloads dependency tree for selected runtime environment", async () => {
    const user = userEvent.setup();

    render(
      <MemoryRouter
        initialEntries={["/package/flask"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    await screen.findByText("A lightweight WSGI framework");
    await user.selectOptions(screen.getByLabelText("Python version"), "3.11");
    await user.selectOptions(screen.getByLabelText("OS"), "windows");

    await waitFor(() => {
      expect(
        api.fetchDependencies.mock.calls.some(
          ([ecosystem, packageName, version, params]) =>
            ecosystem === "pypi" &&
            packageName === "flask" &&
            version === "3.1.3" &&
            params?.include_optional === false &&
            params?.python_version === "3.11" &&
            params?.os === "windows"
        )
      ).toBe(true);
    });
  });

  it("renders dependency conditions and install hints", async () => {
    api.fetchDependencies.mockResolvedValueOnce({
      package: "flask",
      version: "3.1.3",
      depth: 1,
      dependencies: [
        {
          name: "argon2-cffi",
          constraint: ">=23.1.0",
          dependency_type: "runtime",
          extra_name: null,
          environment_markers: {
            expression: "python_version < \"3.13\" and extra == \"argon2\" and platform_system == \"Linux\" and platform_machine == \"x86_64\"",
            conditions: [
              { marker: "python_version", op: "<", value: "3.13" },
              { marker: "extra", op: "==", value: "argon2" },
              { marker: "platform_system", op: "==", value: "Linux" },
              { marker: "platform_machine", op: "==", value: "x86_64" },
            ],
          },
          resolved_version: "25.1.0",
          cycle: false,
          children: [],
        },
      ],
    });

    render(
      <MemoryRouter
        initialEntries={["/package/flask"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText("argon2-cffi")).toBeInTheDocument();
    expect(screen.getByText(">=23.1.0")).toBeInTheDocument();
    expect(screen.getByText("25.1.0")).toBeInTheDocument();
    expect(screen.getByText("Python < 3.13")).toBeInTheDocument();
    expect(screen.getAllByText("Linux").length).toBeGreaterThan(0);
    expect(screen.getByText("x86_64")).toBeInTheDocument();
    expect(screen.getByText("extra argon2")).toBeInTheDocument();
    expect(screen.getByText("Enable with")).toBeInTheDocument();
    expect(screen.getByText("flask[argon2]")).toBeInTheDocument();
    expect(screen.queryByText(/platform_system ==/i)).not.toBeInTheDocument();
  });

  it("reads selected filters from query params", async () => {
    render(
      <MemoryRouter
        initialEntries={["/package/flask?version=3.1.2&python=3.11&os=windows&include_optional=1"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    await screen.findByText("A lightweight WSGI framework");

    await waitFor(() => {
      expect(api.fetchDependencies).toHaveBeenCalledWith("pypi", "flask", "3.1.2", {
        include_optional: true,
        python_version: "3.11",
        os: "windows",
      });
    });
  });

  it("collapses a branch and the whole tree", async () => {
    const user = userEvent.setup();
    api.fetchDependencies.mockResolvedValueOnce({
      package: "flask",
      version: "3.1.3",
      depth: 2,
      dependencies: [
        {
          name: "werkzeug",
          constraint: ">=3.1.0",
          dependency_type: "runtime",
          extra_name: null,
          environment_markers: null,
          resolved_version: "3.1.0",
          cycle: false,
          children: [
            {
              name: "markupsafe",
              constraint: ">=2.1.1",
              dependency_type: "runtime",
              extra_name: null,
              environment_markers: null,
              resolved_version: "3.0.3",
              cycle: false,
              children: [],
            },
          ],
        },
      ],
    });

    render(
      <MemoryRouter
        initialEntries={["/package/flask"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText("markupsafe")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Collapse werkzeug"));
    expect(screen.queryByText("markupsafe")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Collapse tree" }));
    expect(screen.queryByText("werkzeug")).toBeInTheDocument();
    expect(screen.queryByText("markupsafe")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Expand tree" }));
    expect(await screen.findByText("markupsafe")).toBeInTheDocument();
  });

  function renderPackagePage() {
    render(
      <MemoryRouter
        initialEntries={["/package/flask"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );
  }

  it("renders package sources with primary, disabled and missing marks", async () => {
    const base = await api.fetchPackage();
    api.fetchPackage.mockResolvedValueOnce({
      ...base,
      sources: [
        {
          registry: "pypi",
          enabled: true,
          primary: true,
          first_seen_at: "2026-01-01T00:00:00Z",
          checked_at: "2026-03-01T00:00:00Z",
          missing_since: null,
        },
        {
          registry: "pytorch-cpu",
          enabled: false,
          primary: false,
          first_seen_at: "2026-01-02T00:00:00Z",
          checked_at: null,
          missing_since: "2026-02-01T00:00:00Z",
        },
      ],
    });
    renderPackagePage();

    expect(await screen.findByRole("heading", { name: "Sources" })).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem").filter((li) => li.classList.length === 0 && li.querySelector("code"));
    const pypi = rows.find((li) => li.textContent.startsWith("pypi"));
    const torch = rows.find((li) => li.textContent.startsWith("pytorch-cpu"));
    expect(pypi).toHaveTextContent("primary");
    expect(pypi).toHaveTextContent("checked");
    expect(pypi).not.toHaveTextContent("disabled");
    expect(torch).toHaveTextContent("disabled");
    expect(torch).toHaveTextContent("missing since");
    expect(torch).not.toHaveTextContent("primary");
    expect(torch).not.toHaveTextContent("checked");
  });

  it.each([["absent", undefined], ["empty", []]])("renders no sources block when sources are %s", async (_label, sources) => {
    const base = await api.fetchPackage();
    api.fetchPackage.mockResolvedValueOnce({ ...base, sources });
    renderPackagePage();
    await screen.findByText("A lightweight WSGI framework");
    expect(screen.queryByRole("heading", { name: "Sources" })).not.toBeInTheDocument();
  });

  it("shows the unknown-dependencies message when the tree root has no metadata", async () => {
    api.fetchDependencies.mockResolvedValueOnce({
      package: "flask",
      version: "3.1.3",
      profile: "default",
      depth: 0,
      dependencies_unknown: true,
      dependencies: [],
    });
    renderPackagePage();

    expect(
      await screen.findByText("Dependencies unknown: the source has no metadata for this version")
    ).toBeInTheDocument();
    expect(screen.queryByText("No runtime dependencies for this environment.")).not.toBeInTheDocument();
  });

  it("marks a tree node whose version has unknown dependencies", async () => {
    api.fetchDependencies.mockResolvedValueOnce({
      package: "flask",
      version: "3.1.3",
      profile: "default",
      depth: 1,
      dependencies_unknown: false,
      dependencies: [
        {
          name: "werkzeug",
          constraint: ">=3.1.0",
          dependency_type: "runtime",
          extra_name: null,
          environment_markers: null,
          resolved_version: "3.1.0",
          cycle: false,
          dependencies_unknown: true,
          children: [],
        },
      ],
    });
    renderPackagePage();

    expect(await screen.findByText("werkzeug")).toBeInTheDocument();
    expect(screen.getByText("dependencies unknown")).toHaveClass("tag", "warn");
    expect(screen.queryByText(/^Dependencies unknown:/)).not.toBeInTheDocument();
  });

  it("shows version sources and the unknown mark when dependencies are unavailable", async () => {
    api.fetchVersion.mockResolvedValue({
      name: "flask",
      version: "3.1.3",
      status: "active",
      dependencies_state: "unavailable",
      sources: [
        { registry: "pypi", enabled: true, status: "active", first_seen_at: "2026-01-01T00:00:00Z" },
        { registry: "pytorch-cpu", enabled: true, status: "yanked", first_seen_at: "2026-01-02T00:00:00Z" },
      ],
    });
    renderPackagePage();

    expect(await screen.findByText("pypi: active")).toBeInTheDocument();
    expect(screen.getByText("pytorch-cpu: yanked")).toBeInTheDocument();
    expect(screen.getByText("dependencies unknown")).toBeInTheDocument();
    expect(api.fetchVersion).toHaveBeenCalledWith("pypi", "flask", "3.1.3");
  });

  it("shows no unknown mark for a version with hydrated dependencies", async () => {
    renderPackagePage();
    await screen.findByText("werkzeug");
    await waitFor(() => expect(api.fetchVersion).toHaveBeenCalled());
    expect(screen.queryByText("dependencies unknown")).not.toBeInTheDocument();
  });

  describe("by ecosystem", () => {
    function renderApp(path) {
      render(
        <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <App />
        </MemoryRouter>
      );
    }

    function mockAngularCore() {
      api.fetchPackage.mockResolvedValue({
        ecosystem: "npm",
        name: "core",
        normalized_name: "core",
        namespace: "angular",
        purl: "pkg:npm/%40angular/core",
        description: "Angular - the core framework",
        license_spdx: "MIT",
        latest_version: "19.0.0",
        sources: [],
      });
      api.fetchVersions.mockResolvedValue({
        data: [{ version: "19.0.0", status: "active", release_date: "2026-01-01T00:00:00Z" }],
        cursor: null,
        has_more: false,
      });
      api.fetchDependencies.mockResolvedValue({ package: "core", version: "19.0.0", depth: 0, dependencies: [] });
    }

    it("opens a scoped npm package by its purl route and shows its purl", async () => {
      mockAngularCore();
      renderApp("/package/npm/@angular/core");

      expect(await screen.findByText("Angular - the core framework")).toBeInTheDocument();
      expect(screen.getByText("pkg:npm/%40angular/core")).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("npm", "@angular/core");
      expect(api.fetchVersions).toHaveBeenCalledWith("npm", "@angular/core", { limit: 30 });
      expect(api.fetchVulnerabilities).toHaveBeenCalledWith("npm", "@angular/core");
      await waitFor(() => {
        expect(api.fetchVersion).toHaveBeenCalledWith("npm", "@angular/core", "19.0.0");
      });
    });

    it("shows the npm environment fields and sends them to the tree", async () => {
      const user = userEvent.setup();
      mockAngularCore();
      renderApp("/package/npm/@angular/core");

      await screen.findByText("Angular - the core framework");
      expect(screen.getByLabelText("Node.js version")).toBeInTheDocument();
      expect(screen.getByLabelText("CPU")).toBeInTheDocument();
      expect(screen.getByLabelText("OS")).toBeInTheDocument();
      expect(screen.queryByLabelText("Python version")).not.toBeInTheDocument();
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenCalledWith("npm", "@angular/core", "19.0.0", {
          include_optional: false,
          node_version: "20.0.0",
          os: "linux",
          cpu: "x64",
        });
      });

      await user.selectOptions(screen.getByLabelText("OS"), "win32");
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("npm", "@angular/core", "19.0.0", {
          include_optional: false,
          node_version: "20.0.0",
          os: "win32",
          cpu: "x64",
        });
      });
    });

    it("keeps the Python fields and the old route for PyPI", async () => {
      renderApp("/package/flask");

      expect(await screen.findByText("A lightweight WSGI framework")).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("pypi", "flask");
      expect(screen.getByLabelText("Python version")).toBeInTheDocument();
      expect(screen.getByLabelText("OS")).toBeInTheDocument();
      expect(screen.queryByLabelText("Node.js version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("CPU")).not.toBeInTheDocument();
    });

    it("opens PyPI by the ecosystem route too", async () => {
      renderApp("/package/pypi/flask");

      expect(await screen.findByText("A lightweight WSGI framework")).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("pypi", "flask");
      expect(screen.getByLabelText("Python version")).toBeInTheDocument();
    });
  });

  describe("crates", () => {
    function renderApp(path) {
      render(
        <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <App />
        </MemoryRouter>
      );
    }

    function mockSerdeJson() {
      api.fetchPackage.mockResolvedValue({
        ecosystem: "crates",
        name: "serde_json",
        normalized_name: "serde_json",
        namespace: "",
        purl: "pkg:cargo/serde_json",
        description: "A JSON serialization file format",
        homepage_url: "https://serde.rs",
        repository_url: "https://github.com/serde-rs/json",
        documentation_url: "https://docs.rs/serde_json",
        license_spdx: "MIT OR Apache-2.0",
        latest_version: "1.0.140",
        sources: [],
      });
      api.fetchVersions.mockResolvedValue({
        data: [
          { version: "1.0.140", status: "active", release_date: "2026-03-01T10:00:00Z" },
          { version: "1.0.139", status: "yanked", release_date: "2026-02-01T10:00:00Z" },
        ],
        cursor: null,
        has_more: false,
      });
      api.fetchVersion.mockResolvedValue({
        name: "serde_json",
        purl: "pkg:cargo/serde_json@1.0.140",
        version: "1.0.140",
        status: "active",
        features: {
          std: ["serde/std"],
          default: ["std"],
          raw_value: [],
          preserve_order: ["dep:indexmap", "std"],
          dependencies_hydrated: true,
        },
        dependencies_state: "hydrated",
        sources: [],
      });
      api.fetchDependencies.mockResolvedValue({ package: "serde_json", version: "1.0.140", depth: 0, dependencies: [] });
    }

    it("opens a crate by its route and shows purl, links, versions with dates and yanked ones", async () => {
      mockSerdeJson();
      renderApp("/package/crates/serde_json");

      expect(await screen.findByText("A JSON serialization file format")).toBeInTheDocument();
      expect(screen.getByText("pkg:cargo/serde_json")).toBeInTheDocument();
      expect(screen.getByText("crates.io", { selector: ".eco-badge" })).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("crates", "serde_json");
      expect(api.fetchVersions).toHaveBeenCalledWith("crates", "serde_json", { limit: 30 });
      expect(api.fetchVulnerabilities).toHaveBeenCalledWith("crates", "serde_json");
      await waitFor(() => expect(api.fetchVersion).toHaveBeenCalledWith("crates", "serde_json", "1.0.140"));

      expect(screen.getByRole("link", { name: "https://serde.rs" })).toHaveAttribute("href", "https://serde.rs");
      expect(screen.getByRole("link", { name: "https://github.com/serde-rs/json" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "https://docs.rs/serde_json" })).toHaveAttribute(
        "href",
        "https://docs.rs/serde_json"
      );
      expect(screen.getByText((content) => content.includes("MIT OR Apache-2.0"))).toBeInTheDocument();

      expect(screen.getByText("2026-03-01")).toBeInTheDocument();
      expect(screen.getByText("2026-02-01")).toBeInTheDocument();
      expect(screen.getByText("yanked", { selector: ".tag" })).toHaveClass("warn");
      expect(screen.getByText("active", { selector: ".tag" })).not.toHaveClass("warn");
    });

    it("shows the crates environment fields, not the Python or npm ones, and sends them to the tree", async () => {
      const user = userEvent.setup();
      mockSerdeJson();
      renderApp("/package/crates/serde_json");

      await screen.findByText("A JSON serialization file format");
      const target = screen.getByLabelText("Target");
      expect(target).toHaveValue("x86_64-unknown-linux-gnu");
      expect(within(target).getAllByRole("option")).toHaveLength(21);
      expect(screen.getByLabelText("Features")).toHaveValue("");
      expect(screen.getByLabelText("Default features")).toBeChecked();
      expect(screen.queryByLabelText("Python version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Node.js version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("OS")).not.toBeInTheDocument();
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenCalledWith("crates", "serde_json", "1.0.140", {
          include_optional: false,
          target: "x86_64-unknown-linux-gnu",
          features: "",
          default_features: true,
        });
      });

      await user.selectOptions(target, "x86_64-pc-windows-msvc");
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("crates", "serde_json", "1.0.140", {
          include_optional: false,
          target: "x86_64-pc-windows-msvc",
          features: "",
          default_features: true,
        });
      });

      // Features are sent once typed, on Enter, comma-separated and trimmed.
      const callsBefore = api.fetchDependencies.mock.calls.length;
      await user.type(screen.getByLabelText("Features"), "std, raw_value ,");
      expect(api.fetchDependencies.mock.calls).toHaveLength(callsBefore);
      await user.keyboard("{Enter}");
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("crates", "serde_json", "1.0.140", {
          include_optional: false,
          target: "x86_64-pc-windows-msvc",
          features: "std,raw_value",
          default_features: true,
        });
      });

      await user.click(screen.getByLabelText("Default features"));
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("crates", "serde_json", "1.0.140", {
          include_optional: false,
          target: "x86_64-pc-windows-msvc",
          features: "std,raw_value",
          default_features: false,
        });
      });
    });

    it("reads target, features and default features from the query", async () => {
      mockSerdeJson();
      renderApp("/package/crates/serde_json?version=1.0.139&target=aarch64-apple-darwin&features=std&default_features=0");

      await screen.findByText("A JSON serialization file format");
      expect(screen.getByLabelText("Target")).toHaveValue("aarch64-apple-darwin");
      expect(screen.getByLabelText("Features")).toHaveValue("std");
      expect(screen.getByLabelText("Default features")).not.toBeChecked();
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenCalledWith("crates", "serde_json", "1.0.139", {
          include_optional: false,
          target: "aarch64-apple-darwin",
          features: "std",
          default_features: false,
        });
      });
    });

    it("does not send an unknown target from the query", async () => {
      mockSerdeJson();
      renderApp("/package/crates/serde_json?target=sparc-sun-solaris");

      await screen.findByText("A JSON serialization file format");
      await waitFor(() => {
        expect(api.fetchDependencies).toHaveBeenCalledWith(
          "crates",
          "serde_json",
          "1.0.140",
          expect.objectContaining({ target: "x86_64-unknown-linux-gnu" })
        );
      });
    });

    it("marks platform-specific, build and optional edges", async () => {
      mockSerdeJson();
      api.fetchDependencies.mockResolvedValue({
        package: "serde_json",
        version: "1.0.140",
        depth: 1,
        dependencies: [
          {
            name: "winapi",
            constraint: "^0.3",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: { rust_cfg: "cfg(windows)" },
            resolved_version: "0.3.9",
            cycle: false,
            children: [],
          },
          {
            name: "cc",
            constraint: "^1.0",
            dependency_type: "build",
            extra_name: null,
            environment_markers: null,
            resolved_version: "1.2.1",
            cycle: false,
            children: [],
          },
          {
            name: "itoa",
            constraint: "^1.0",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: null,
            resolved_version: "1.0.15",
            cycle: false,
            children: [],
          },
        ],
      });
      renderApp("/package/crates/serde_json");

      expect(await screen.findByText("winapi")).toBeInTheDocument();
      expect(screen.getByText("cfg(windows)")).toBeInTheDocument();
      expect(screen.getAllByText("build dependency")).toHaveLength(1);
      const cc = screen.getByText("cc").closest("li");
      expect(within(cc).getByText("build dependency")).toBeInTheDocument();
      const itoa = screen.getByText("itoa").closest("li");
      expect(within(itoa).queryByText("build dependency")).not.toBeInTheDocument();
      expect(within(itoa).queryByText(/cfg\(/)).not.toBeInTheDocument();
    });

    it("lists the features of the selected version without the internal key", async () => {
      const user = userEvent.setup();
      mockSerdeJson();
      renderApp("/package/crates/serde_json");

      expect(await screen.findByRole("heading", { name: "Features of 1.0.140" })).toBeInTheDocument();
      const items = document.querySelectorAll(".feature-item");
      // `default` first, then alphabetical.
      expect([...items].map((item) => item.querySelector("dt").textContent)).toEqual([
        "default",
        "preserve_order",
        "raw_value",
        "std",
      ]);
      expect(screen.getByText("serde/std")).toBeInTheDocument();
      expect(screen.getByText("dep:indexmap")).toBeInTheDocument();
      expect(screen.getByText("enables nothing")).toBeInTheDocument();
      expect(screen.queryByText("dependencies_hydrated")).not.toBeInTheDocument();

      api.fetchVersion.mockResolvedValue({
        name: "serde_json",
        purl: "pkg:cargo/serde_json@1.0.139",
        version: "1.0.139",
        status: "yanked",
        features: {},
        dependencies_state: "hydrated",
        sources: [],
      });
      await user.selectOptions(screen.getByLabelText("Version"), "1.0.139");
      expect(await screen.findByRole("heading", { name: "Features of 1.0.139" })).toBeInTheDocument();
      expect(screen.getByText("No features.")).toBeInTheDocument();
    });

    it("sends no crates parameters for PyPI or npm and shows no features block", async () => {
      renderApp("/package/flask");
      await screen.findByText("werkzeug");
      await waitFor(() => expect(api.fetchVersion).toHaveBeenCalled());
      expect(screen.queryByRole("heading", { name: /^Features of/ })).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Target")).not.toBeInTheDocument();
      for (const [, , , params] of api.fetchDependencies.mock.calls) {
        expect(params).not.toHaveProperty("target");
        expect(params).not.toHaveProperty("features");
        expect(params).not.toHaveProperty("default_features");
      }
    });
  });

  describe("go", () => {
    const MODULE = "github.com/BurntSushi/toml";

    function renderApp(path) {
      render(
        <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <App />
        </MemoryRouter>
      );
    }

    function mockModule(overrides = {}) {
      api.fetchPackage.mockResolvedValue({
        ecosystem: "go",
        name: MODULE,
        normalized_name: MODULE,
        namespace: "",
        purl: "pkg:golang/github.com/BurntSushi/toml",
        description: null,
        homepage_url: null,
        repository_url: null,
        documentation_url: null,
        license_raw: null,
        license_spdx: null,
        latest_version: "v1.4.0",
        sources: [],
        ...overrides,
      });
      api.fetchVersions.mockResolvedValue({
        data: [
          { version: "v1.4.1-0.20261014143641-f0251eb4aa3e", status: "active", release_date: "2026-10-14T14:36:41Z" },
          { version: "v1.4.0", status: "active", release_date: "2026-03-01T10:00:00Z" },
          { version: "v1.3.0", status: "yanked", status_reason: "wrong tag, use v1.3.1", release_date: "2026-02-01T10:00:00Z" },
          { version: "v2.0.0+incompatible", status: "active", release_date: "2025-12-01T10:00:00Z" },
          { version: "v0.0.0-20261014143641-f0251eb4aa3e", status: "active", release_date: "2025-11-01T10:00:00Z" },
        ],
        cursor: null,
        has_more: false,
      });
      api.fetchVersion.mockResolvedValue({
        name: MODULE,
        purl: "pkg:golang/github.com/BurntSushi/toml@v1.4.0",
        version: "v1.4.0",
        status: "active",
        features: null,
        dependencies_state: "hydrated",
        sources: [],
      });
      api.fetchDependencies.mockResolvedValue({
        package: MODULE,
        version: "v1.4.0",
        depth: 1,
        dependencies: [
          {
            name: "github.com/BurntSushi/toml-test",
            constraint: "v1.0.0",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: null,
            details: {},
            resolved_version: "v1.0.0",
            cycle: false,
            children: [],
          },
          {
            name: "golang.org/x/sys",
            constraint: "v0.20.0",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: null,
            details: { indirect: true },
            resolved_version: "v0.20.0",
            cycle: false,
            children: [],
          },
        ],
      });
    }

    it("opens a module by a multi-segment route, keeping the path and its case", async () => {
      mockModule();
      renderApp(`/package/go/${MODULE}`);

      expect(await screen.findByText("pkg:golang/github.com/BurntSushi/toml")).toBeInTheDocument();
      expect(screen.getByText("Go", { selector: ".eco-badge" })).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("go", MODULE);
      expect(api.fetchVersions).toHaveBeenCalledWith("go", MODULE, { limit: 30 });
      expect(api.fetchVulnerabilities).toHaveBeenCalledWith("go", MODULE);
      await waitFor(() => expect(api.fetchVersion).toHaveBeenCalledWith("go", MODULE, "v1.4.0"));
    });

    it("opens a module with a major version suffix", async () => {
      mockModule({ name: "github.com/redis/go-redis/v9", normalized_name: "github.com/redis/go-redis/v9" });
      renderApp("/package/go/github.com/redis/go-redis/v9");

      await screen.findByText("pkg:golang/github.com/BurntSushi/toml");
      expect(api.fetchPackage).toHaveBeenCalledWith("go", "github.com/redis/go-redis/v9");
    });

    it("shows no empty description, license or link blocks", async () => {
      mockModule();
      renderApp(`/package/go/${MODULE}`);

      await screen.findByText("pkg:golang/github.com/BurntSushi/toml");
      expect(screen.queryByText("No description")).not.toBeInTheDocument();
      expect(screen.queryByText(/License/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Homepage/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Repo/)).not.toBeInTheDocument();
      expect(screen.queryByText("n/a")).not.toBeInTheDocument();
      expect(screen.getByText(/Latest/)).toBeInTheDocument();
    });

    it("shows the license and links of a module when the catalog has them", async () => {
      mockModule({ license_spdx: "MIT", repository_url: "https://github.com/BurntSushi/toml" });
      renderApp(`/package/go/${MODULE}`);

      await screen.findByText("pkg:golang/github.com/BurntSushi/toml");
      expect(screen.getByText((content) => content.includes("MIT"))).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "https://github.com/BurntSushi/toml" })).toBeInTheDocument();
      expect(screen.queryByText(/Homepage/)).not.toBeInTheDocument();
    });

    it("marks pseudo-versions and retracted versions with their reason, and keeps +incompatible as is", async () => {
      mockModule();
      renderApp(`/package/go/${MODULE}`);

      await screen.findByText("pkg:golang/github.com/BurntSushi/toml");
      const items = [...document.querySelectorAll(".inline-list > li")];
      const row = (version) => items.find((item) => item.textContent.startsWith(version));

      expect(within(row("v1.4.1-0.20261014143641-f0251eb4aa3e")).getByText("pseudo-version")).toBeInTheDocument();
      expect(within(row("v0.0.0-20261014143641-f0251eb4aa3e")).getByText("pseudo-version")).toBeInTheDocument();
      expect(within(row("v1.4.0")).queryByText("pseudo-version")).not.toBeInTheDocument();

      const retracted = row("v1.3.0");
      expect(within(retracted).getByText("retracted")).toHaveClass("warn");
      expect(within(retracted).getByText("wrong tag, use v1.3.1")).toBeInTheDocument();

      expect(within(row("v2.0.0+incompatible")).queryByText("pseudo-version")).not.toBeInTheDocument();
      expect(screen.getAllByText("v2.0.0+incompatible").length).toBeGreaterThan(0);
      expect(screen.getByText("2026-10-14", { exact: false })).toBeInTheDocument();
    });

    it("shows a deprecation banner only when the module is deprecated", async () => {
      mockModule({ deprecation: { message: "use github.com/pelletier/go-toml/v2 instead" } });
      renderApp(`/package/go/${MODULE}`);

      const banner = await screen.findByRole("alert");
      expect(banner).toHaveTextContent("Deprecated: use github.com/pelletier/go-toml/v2 instead");
    });

    it("shows no deprecation banner for a module that is not deprecated", async () => {
      mockModule({ deprecation: null });
      renderApp(`/package/go/${MODULE}`);

      await screen.findByText("pkg:golang/github.com/BurntSushi/toml");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("has no environment form and sends no tree parameters", async () => {
      mockModule();
      renderApp(`/package/go/${MODULE}`);

      await screen.findByText("golang.org/x/sys");
      expect(screen.queryByLabelText("Python version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Node.js version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Target")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("OS")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("CPU")).not.toBeInTheDocument();
      expect(screen.queryByText("include optional")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Version")).toBeInTheDocument();
      expect(api.fetchDependencies).toHaveBeenCalledWith("go", MODULE, "v1.4.0", {});
    });

    it("tags indirect requirements", async () => {
      mockModule();
      renderApp(`/package/go/${MODULE}`);

      await screen.findByText("golang.org/x/sys");
      const direct = screen.getByText("github.com/BurntSushi/toml-test").closest(".dep-tree-item");
      const indirect = screen.getByText("golang.org/x/sys").closest(".dep-tree-item");
      expect(within(indirect).getByText("indirect")).toBeInTheDocument();
      expect(within(direct).queryByText("indirect")).not.toBeInTheDocument();
    });

    it("opens the version of the URL and sends it to the tree", async () => {
      mockModule();
      render(
        <MemoryRouter
          initialEntries={[`/package/go/${MODULE}?version=v1.3.0`]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <Routes>
            <Route path="/package/:ecosystem/*" element={<PackagePage />} />
          </Routes>
        </MemoryRouter>
      );

      await waitFor(() => expect(api.fetchDependencies).toHaveBeenCalledWith("go", MODULE, "v1.3.0", {}));
    });

    it("shows the retraction reason of the selected version", async () => {
      mockModule();
      api.fetchVersion.mockResolvedValue({
        name: MODULE,
        purl: "pkg:golang/github.com/BurntSushi/toml@v1.3.0",
        version: "v1.3.0",
        status: "yanked",
        status_reason: "wrong tag, use v1.3.1",
        dependencies_state: "hydrated",
        sources: [],
      });
      renderApp(`/package/go/${MODULE}?version=v1.3.0`);

      expect(await screen.findByText("Retracted: wrong tag, use v1.3.1")).toBeInTheDocument();
    });

    it("does not call a yanked version of another ecosystem retracted", async () => {
      api.fetchVersions.mockResolvedValue({
        data: [{ version: "3.1.3", status: "yanked", status_reason: "broken", release_date: "2026-02-01T10:00:00Z" }],
        cursor: null,
        has_more: false,
      });
      renderApp("/package/flask");

      expect(await screen.findByText("yanked", { selector: ".tag" })).toBeInTheDocument();
      expect(screen.queryByText("retracted")).not.toBeInTheDocument();
      expect(screen.queryByText("broken")).not.toBeInTheDocument();
    });
  });

  describe("nuget", () => {
    const ID = "newtonsoft.json";

    function renderApp(path) {
      render(
        <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <App />
        </MemoryRouter>
      );
    }

    function mockPackage(overrides = {}) {
      api.fetchPackage.mockResolvedValue({
        ecosystem: "nuget",
        name: "Newtonsoft.Json",
        normalized_name: ID,
        namespace: "",
        purl: "pkg:nuget/newtonsoft.json",
        description: "Json.NET is a popular high-performance JSON framework for .NET",
        homepage_url: "https://www.newtonsoft.com/json",
        repository_url: null,
        documentation_url: null,
        license_raw: "MIT",
        license_spdx: "MIT",
        latest_version: "13.0.3",
        sources: [],
        ...overrides,
      });
      api.fetchVersions.mockResolvedValue({
        data: [
          { version: "13.0.3", status: "active", release_date: "2023-03-08T10:00:00Z" },
          { version: "13.0.3-beta1", status: "unlisted", release_date: null },
          { version: "13.0.2", status: "unlisted", release_date: "2023-02-01T10:00:00Z" },
          { version: "12.0.3.1", status: "active", release_date: "2019-11-09T10:00:00Z", deprecation: { message: "use 12.0.4" } },
          { version: "1.0.0-BETA", status: "active", release_date: "2011-01-01T10:00:00Z" },
        ],
        cursor: null,
        has_more: false,
      });
      api.fetchVersion.mockResolvedValue({
        name: "Newtonsoft.Json",
        purl: "pkg:nuget/newtonsoft.json@13.0.3",
        version: "13.0.3",
        status: "active",
        features: null,
        dependencies_state: "hydrated",
        sources: [],
      });
      api.fetchDependencies.mockResolvedValue({
        package: ID,
        version: "13.0.3",
        depth: 1,
        dependencies: [
          {
            name: "microsoft.csharp",
            constraint: "4.3.0",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: { dotnet_target_framework: ".NETStandard2.0" },
            details: { groups: [".NETStandard2.0"] },
            resolved_version: "4.3.0",
            cycle: false,
            children: [],
          },
          {
            name: "system.memory",
            constraint: "[4.5.5, )",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: null,
            resolved_version: "4.5.5",
            cycle: false,
            children: [],
          },
        ],
      });
    }

    it("opens a package by its lowercase id and shows the purl, the license and the registry's spelling", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}`);

      expect(await screen.findByText("pkg:nuget/newtonsoft.json")).toBeInTheDocument();
      expect(screen.getByText("NuGet", { selector: ".eco-badge" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Package: Newtonsoft.Json");
      expect(screen.getByText((content) => content.includes("MIT"))).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("nuget", ID);
      expect(api.fetchVersions).toHaveBeenCalledWith("nuget", ID, { limit: 30 });
      expect(api.fetchVulnerabilities).toHaveBeenCalledWith("nuget", ID);
      await waitFor(() => expect(api.fetchVersion).toHaveBeenCalledWith("nuget", ID, "13.0.3"));
    });

    it("shows a license URL as a link when the package has no expression", async () => {
      mockPackage({ license_spdx: null, license_raw: "https://licenses.nuget.org/MIT" });
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("pkg:nuget/newtonsoft.json");
      expect(screen.getByText((content) => content.includes("https://licenses.nuget.org/MIT"))).toBeInTheDocument();
    });

    it("marks unlisted versions, shows four-part versions and dates as is", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("pkg:nuget/newtonsoft.json");
      const items = [...document.querySelectorAll(".inline-list > li")];
      const row = (version) => items.find((item) => item.textContent.startsWith(version));

      expect(within(row("13.0.2")).getByText("unlisted")).toBeInTheDocument();
      expect(within(row("13.0.2")).getByText("2023-02-01")).toBeInTheDocument();
      // An unlisted version may have no date.
      expect(within(row("13.0.3-beta1")).getByText("unlisted")).toBeInTheDocument();
      expect(row("13.0.3-beta1").querySelector(".version-date")).toBeNull();
      expect(within(row("13.0.3")).queryByText("unlisted")).not.toBeInTheDocument();
      expect(within(row("13.0.3")).getByText("2023-03-08")).toBeInTheDocument();
      expect(within(row("12.0.3.1")).getByText("active")).toBeInTheDocument();
      expect(row("1.0.0-BETA")).toBeTruthy();
      expect(screen.getByRole("option", { name: "12.0.3.1" })).toBeInTheDocument();
    });

    it("marks a deprecated version with its message when the version data carries it", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("pkg:nuget/newtonsoft.json");
      const deprecated = [...document.querySelectorAll(".inline-list > li")].find((item) => item.textContent.startsWith("12.0.3.1"));
      expect(within(deprecated).getByText("deprecated")).toBeInTheDocument();
      expect(within(deprecated).getByText("use 12.0.4")).toBeInTheDocument();
      const plain = document.querySelector(".inline-list > li");
      expect(plain.textContent.startsWith("13.0.3active")).toBe(true);
      expect(within(plain).queryByText("deprecated")).not.toBeInTheDocument();
    });

    it("shows a deprecation banner only when the package is deprecated", async () => {
      mockPackage({ deprecation: { message: "Legacy, CriticalBugs" } });
      renderApp(`/package/nuget/${ID}`);

      expect(await screen.findByRole("alert")).toHaveTextContent("Deprecated: Legacy, CriticalBugs");
    });

    it("shows no deprecation banner for a package that is not deprecated", async () => {
      mockPackage({ deprecation: null });
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("pkg:nuget/newtonsoft.json");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("explains an unlisted selected version and shows the deprecation of the selected version", async () => {
      mockPackage();
      api.fetchVersion.mockResolvedValue({
        name: "Newtonsoft.Json",
        purl: "pkg:nuget/newtonsoft.json@12.0.3.1",
        version: "12.0.3.1",
        status: "unlisted",
        deprecation: { message: "use 12.0.4" },
        dependencies_state: "hydrated",
        sources: [],
      });
      renderApp(`/package/nuget/${ID}?version=12.0.3.1`);

      expect(await screen.findByText(/^Unlisted:/)).toBeInTheDocument();
      expect(screen.getByText("Deprecated: use 12.0.4", { selector: "p" })).toBeInTheDocument();
    });

    it("shows a target framework selector with net8.0 selected and sends only target_framework", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("system.memory");
      const select = screen.getByLabelText("Target framework");
      expect(select).toHaveValue("net8.0");
      const options = within(select).getAllByRole("option").map((option) => option.value);
      expect(options).toEqual(
        expect.arrayContaining(["net5.0", "net8.0", "net11.0", "netstandard1.0", "netstandard1.6", "netstandard2.0", "netstandard2.1"])
      );
      expect(options).not.toContain("net48");
      expect(screen.queryByLabelText("Python version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Node.js version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Target")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("OS")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("CPU")).not.toBeInTheDocument();
      expect(screen.queryByText("include optional")).not.toBeInTheDocument();
      expect(api.fetchDependencies).toHaveBeenCalledWith("nuget", ID, "13.0.3", { target_framework: "net8.0" });
    });

    it("reloads the tree for another target framework", async () => {
      mockPackage();
      const user = userEvent.setup();
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("system.memory");
      await user.selectOptions(screen.getByLabelText("Target framework"), "netstandard2.0");
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("nuget", ID, "13.0.3", { target_framework: "netstandard2.0" })
      );
    });

    it("reads the target framework from the query", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}?target_framework=net6.0`);
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenCalledWith("nuget", ID, "13.0.3", { target_framework: "net6.0" })
      );
      expect(screen.getByLabelText("Target framework")).toHaveValue("net6.0");
    });

    it("falls back to net8.0 for an unknown target framework in the query", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}?target_framework=net48`);
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenCalledWith("nuget", ID, "13.0.3", { target_framework: "net8.0" })
      );
    });

    it("shows the framework group of an edge", async () => {
      mockPackage();
      renderApp(`/package/nuget/${ID}`);

      await screen.findByText("system.memory");
      const grouped = screen.getByText("microsoft.csharp").closest(".dep-tree-item");
      const ungrouped = screen.getByText("system.memory").closest(".dep-tree-item");
      expect(within(grouped).getByText(".NETStandard2.0")).toHaveClass("cond");
      expect(within(ungrouped).queryByText(/NET/)).not.toBeInTheDocument();
    });

    it("sends no target_framework for the other ecosystems", async () => {
      renderApp("/package/flask");
      await screen.findByText("werkzeug");
      expect(screen.queryByLabelText("Target framework")).not.toBeInTheDocument();
      expect(api.fetchDependencies.mock.calls[0][3]).not.toHaveProperty("target_framework");
    });
  });

  describe("maven", () => {
    const GA = "org.springframework:spring-core";

    function renderApp(path) {
      render(
        <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <App />
        </MemoryRouter>
      );
    }

    function mockPackage(overrides = {}) {
      api.fetchPackage.mockResolvedValue({
        ecosystem: "maven",
        name: GA,
        normalized_name: "spring-core",
        namespace: "org.springframework",
        purl: "pkg:maven/org.springframework/spring-core",
        description: "Spring Core",
        homepage_url: "https://github.com/spring-projects/spring-framework",
        repository_url: null,
        documentation_url: null,
        license_raw: "Apache License, Version 2.0",
        license_spdx: "Apache-2.0",
        latest_version: "6.1.14",
        sources: [],
        ...overrides,
      });
      api.fetchVersions.mockResolvedValue({
        data: [
          { version: "6.1.14", status: "active", release_date: "2024-10-16T10:00:00Z", packaging: "jar" },
          { version: "6.2.0-RC1", status: "active", release_date: "2024-10-10T10:00:00Z" },
          { version: "6.1.13", status: "active", release_date: null },
        ],
        cursor: null,
        has_more: false,
      });
      api.fetchVersion.mockResolvedValue({
        name: GA,
        purl: "pkg:maven/org.springframework/spring-core@6.1.14",
        version: "6.1.14",
        status: "active",
        features: null,
        dependencies_state: "hydrated",
        sources: [],
      });
      api.fetchDependencies.mockResolvedValue({
        package: GA,
        version: "6.1.14",
        depth: 1,
        dependencies: [
          {
            name: "org.springframework:spring-jcl",
            constraint: "6.1.14",
            dependency_type: "runtime",
            extra_name: null,
            environment_markers: null,
            details: null,
            resolved_version: "6.1.14",
            cycle: false,
            children: [],
          },
          {
            name: "io.micrometer:micrometer-observation",
            constraint: "1.12.0",
            dependency_type: "dev",
            extra_name: null,
            environment_markers: null,
            details: {
              exclusions: [
                { group: "commons-logging", artifact: "commons-logging" },
                { group: "log4j", artifact: "log4j" },
              ],
              classifier: "tests",
              type: "test-jar",
            },
            resolved_version: "1.12.0",
            cycle: false,
            children: [],
          },
          {
            name: "jakarta.servlet:jakarta.servlet-api",
            constraint: "6.0.0",
            dependency_type: "provided",
            scope: "provided",
            extra_name: null,
            environment_markers: null,
            resolved_version: "6.0.0",
            cycle: false,
            children: [],
          },
        ],
      });
    }

    it("opens a package by groupId:artifactId and shows the purl, groupId, artifactId and the license", async () => {
      mockPackage();
      renderApp(`/package/maven/${GA}`);

      expect(await screen.findByText("pkg:maven/org.springframework/spring-core")).toBeInTheDocument();
      expect(screen.getByText("Maven", { selector: ".eco-badge" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(`Package: ${GA}`);
      expect(screen.getByText("org.springframework", { selector: "code" })).toBeInTheDocument();
      expect(screen.getByText("spring-core", { selector: "code" })).toBeInTheDocument();
      expect(screen.getByText((content) => content.includes("Apache-2.0"))).toBeInTheDocument();
      expect(api.fetchPackage).toHaveBeenCalledWith("maven", GA);
      expect(api.fetchVersions).toHaveBeenCalledWith("maven", GA, { limit: 30 });
      expect(api.fetchVulnerabilities).toHaveBeenCalledWith("maven", GA);
      await waitFor(() => expect(api.fetchVersion).toHaveBeenCalledWith("maven", GA, "6.1.14"));
    });

    it("falls back to the license name of the POM when there is no SPDX id", async () => {
      mockPackage({ license_spdx: null, license_raw: "Eclipse Public License 1.0" });
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("pkg:maven/org.springframework/spring-core");
      expect(screen.getByText((content) => content.includes("Eclipse Public License 1.0"))).toBeInTheDocument();
    });

    it("shows versions with their dates, qualifiers as they are and the packaging where known", async () => {
      mockPackage();
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("pkg:maven/org.springframework/spring-core");
      const items = [...document.querySelectorAll(".inline-list > li")];
      const row = (version) => items.find((item) => item.textContent.startsWith(version));

      expect(within(row("6.1.14")).getByText("2024-10-16")).toBeInTheDocument();
      expect(within(row("6.1.14")).getByText("jar")).toBeInTheDocument();
      expect(within(row("6.2.0-RC1")).getByText("2024-10-10")).toBeInTheDocument();
      expect(within(row("6.2.0-RC1")).queryByText("jar")).not.toBeInTheDocument();
      expect(row("6.1.13").querySelector(".version-date")).toBeNull();
      expect(screen.getByRole("option", { name: "6.2.0-RC1" })).toBeInTheDocument();
    });

    it("shows the packaging of the selected version when the version data carries it", async () => {
      mockPackage();
      api.fetchVersion.mockResolvedValue({
        name: GA,
        version: "6.1.14",
        status: "active",
        raw_metadata: { packaging: "pom" },
        dependencies_state: "hydrated",
        sources: [],
      });
      renderApp(`/package/maven/${GA}`);

      expect(await screen.findByText(/^Packaging:/)).toHaveTextContent("Packaging: pom");
    });

    it("shows the relocation banner with a link to the package it moved to", async () => {
      mockPackage({
        deprecation: { message: "Relocated to org.springframework:spring-core-next", alternative: "org.springframework:spring-core-next:7.0.0" },
      });
      renderApp(`/package/maven/${GA}`);

      const banner = await screen.findByRole("alert");
      expect(banner).toHaveTextContent(
        "Deprecated: Relocated to org.springframework:spring-core-next (moved to org.springframework:spring-core-next:7.0.0)"
      );
      expect(within(banner).getByRole("link")).toHaveAttribute(
        "href",
        "/package/maven/org.springframework:spring-core-next?version=7.0.0"
      );
    });

    it("shows a relocation without a message and links an alternative without a version", async () => {
      mockPackage({ deprecation: { alternative: "jakarta.servlet:jakarta.servlet-api" } });
      renderApp(`/package/maven/${GA}`);

      const banner = await screen.findByRole("alert");
      expect(banner).toHaveTextContent("Deprecated (moved to jakarta.servlet:jakarta.servlet-api)");
      expect(within(banner).getByRole("link")).toHaveAttribute("href", "/package/maven/jakarta.servlet:jakarta.servlet-api");
    });

    it("shows no banner for an artifact that has not moved", async () => {
      mockPackage({ deprecation: null });
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("pkg:maven/org.springframework/spring-core");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("shows scope checkboxes with compile and runtime selected and sends only scopes", async () => {
      mockPackage();
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("org.springframework:spring-jcl");
      const group = screen.getByRole("group", { name: "Scopes" });
      const boxes = within(group).getAllByRole("checkbox");
      expect(boxes.map((box) => box.parentElement.textContent)).toEqual(["compile", "runtime", "provided", "system", "test"]);
      expect(within(group).getByLabelText("compile")).toBeChecked();
      expect(within(group).getByLabelText("runtime")).toBeChecked();
      expect(within(group).getByLabelText("provided")).not.toBeChecked();
      expect(within(group).getByLabelText("system")).not.toBeChecked();
      expect(within(group).getByLabelText("test")).not.toBeChecked();
      expect(screen.queryByLabelText("Python version")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Target framework")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("OS")).not.toBeInTheDocument();
      expect(screen.queryByText("include optional")).not.toBeInTheDocument();
      expect(api.fetchDependencies).toHaveBeenCalledWith("maven", GA, "6.1.14", { scopes: "compile,runtime" });
    });

    it("reloads the tree with the scopes that were ticked, in a fixed order", async () => {
      mockPackage();
      const user = userEvent.setup();
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("org.springframework:spring-jcl");
      await user.click(screen.getByLabelText("test"));
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("maven", GA, "6.1.14", { scopes: "compile,runtime,test" })
      );
      await user.click(screen.getByLabelText("provided"));
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("maven", GA, "6.1.14", { scopes: "compile,runtime,provided,test" })
      );
      await user.click(screen.getByLabelText("compile"));
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenLastCalledWith("maven", GA, "6.1.14", { scopes: "runtime,provided,test" })
      );
    });

    it("keeps the last selected scope: the API refuses an empty list", async () => {
      mockPackage();
      const user = userEvent.setup();
      renderApp(`/package/maven/${GA}?scopes=test`);

      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenCalledWith("maven", GA, "6.1.14", { scopes: "test" })
      );
      await screen.findByText("org.springframework:spring-jcl");
      await user.click(screen.getByLabelText("test"));
      expect(screen.getByLabelText("test")).toBeChecked();
      expect(api.fetchDependencies).toHaveBeenLastCalledWith("maven", GA, "6.1.14", { scopes: "test" });
    });

    it("reads the scopes from the query, drops unknown ones and falls back to the default", async () => {
      mockPackage();
      renderApp(`/package/maven/${GA}?scopes=system,bogus,compile`);
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenCalledWith("maven", GA, "6.1.14", { scopes: "compile,system" })
      );
      expect(screen.getByLabelText("system")).toBeChecked();
      expect(screen.getByLabelText("runtime")).not.toBeChecked();
    });

    it("falls back to compile,runtime when the query has no known scope", async () => {
      mockPackage();
      renderApp(`/package/maven/${GA}?scopes=bogus`);
      await waitFor(() =>
        expect(api.fetchDependencies).toHaveBeenCalledWith("maven", GA, "6.1.14", { scopes: "compile,runtime" })
      );
    });

    it("shows the scope, exclusions, classifier and type of an edge", async () => {
      mockPackage();
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("org.springframework:spring-jcl");
      const item = (name) => screen.getByText(name).closest(".dep-tree-item");

      const test = item("io.micrometer:micrometer-observation");
      expect(within(test).getByText("scope test")).toHaveClass("cond");
      expect(within(test).getByText("excludes commons-logging:commons-logging, log4j:log4j")).toHaveClass("cond");
      expect(within(test).getByText("classifier tests")).toHaveClass("cond");
      expect(within(test).getByText("type test-jar")).toHaveClass("cond");

      // A scope the API gives wins over the one derived from the dependency type.
      expect(within(item("jakarta.servlet:jakarta.servlet-api")).getByText("scope provided")).toBeInTheDocument();

      // compile and runtime edges are not told apart by dependency_type: no scope label without a scope.
      const plain = item("org.springframework:spring-jcl");
      expect(within(plain).queryByText(/^scope /)).not.toBeInTheDocument();
      expect(within(plain).queryByText(/excludes|classifier|type /)).not.toBeInTheDocument();
    });

    it("shows the edge's own scope when the API gives it", async () => {
      mockPackage();
      api.fetchDependencies.mockResolvedValue({
        package: GA,
        version: "6.1.14",
        depth: 1,
        dependencies: [
          {
            name: "org.springframework:spring-jcl",
            constraint: "6.1.14",
            dependency_type: "runtime",
            scope: "runtime",
            extra_name: null,
            environment_markers: null,
            resolved_version: "6.1.14",
            cycle: false,
            children: [],
          },
        ],
      });
      renderApp(`/package/maven/${GA}`);

      await screen.findByText("org.springframework:spring-jcl");
      expect(screen.getByText("scope runtime")).toHaveClass("cond");
    });

    it("sends no scopes for the other ecosystems", async () => {
      renderApp("/package/flask");
      await screen.findByText("werkzeug");
      expect(screen.queryByRole("group", { name: "Scopes" })).not.toBeInTheDocument();
      expect(api.fetchDependencies.mock.calls[0][3]).not.toHaveProperty("scopes");
    });
  });
});
