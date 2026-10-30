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
});
