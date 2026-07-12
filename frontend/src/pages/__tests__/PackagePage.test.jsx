import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import PackagePage from "../PackagePage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchDependencies: vi.fn(),
  fetchPackage: vi.fn(),
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
      classification: {
        classifier_version: "2.4.0",
        classified_at: "2026-07-12T00:00:00Z",
        version: "3.1.3",
        status: "ok",
        status_reason: "",
        facets: [
          { role: "runtime-component", confidence: "high", evidence: "curated override", provisional: false },
          { role: "dev-service", confidence: "high", evidence: "curated override", provisional: false },
        ],
        capabilities: [{ capability: "cli", confidence: "high", evidence: "console_scripts: flask" }],
        effective_capabilities: [
          { name: "cli", origin: "structural" },
          { name: "tui-app", origin: "user" },
        ],
        annotations: [
          { capability: "tui-app", present: true, version: "", note: "curator note", created_at: "2026-07-12T00:00:00Z" },
        ],
        relations: [],
      },
      registries: [
        { slug: "pypi", registry_type: "public", is_primary: true, name_collision: false, checksum_mismatch: false },
        { slug: "nexus-pypi", registry_type: "corporate", is_primary: false, name_collision: true, checksum_mismatch: false },
      ],
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
      expect(api.fetchDependencies).toHaveBeenCalledWith("flask", "3.1.3", {
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
      expect(api.fetchDependencies).toHaveBeenCalledWith("flask", "3.1.2", {
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
          ([packageName, version, params]) =>
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
      expect(api.fetchDependencies).toHaveBeenCalledWith("flask", "3.1.2", {
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

  it("renders classification block with roles, effective capabilities and registry flags", async () => {
    render(
      <MemoryRouter initialEntries={["/package/flask"]}>
        <Routes>
          <Route path="/package/:name" element={<PackagePage />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => expect(api.fetchPackage).toHaveBeenCalled());

    expect(await screen.findByText("Classification")).toBeInTheDocument();
    expect(screen.getByText("runtime-component")).toBeInTheDocument();
    expect(screen.getByText("dev-service")).toBeInTheDocument();
    // эффективное свойство от куратора помечено
    expect(screen.getByText("tui-app")).toBeInTheDocument();
    expect(screen.getByText("(curated)")).toBeInTheDocument();
    // реестры и флаг коллизии имени
    expect(screen.getByText("nexus-pypi")).toBeInTheDocument();
    expect(screen.getByText("name collision")).toBeInTheDocument();
  });

});
