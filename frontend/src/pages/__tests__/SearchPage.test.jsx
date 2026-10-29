import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import SearchPage from "../SearchPage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchPackages: vi.fn(),
  searchPackages: vi.fn(),
}));

const RAW_BINARY_DESCRIPTION = `binary
======

.. image:: https://img.shields.io/pypi/v/binary.svg

binary provides a **bug-free** and easy way to convert between and within binary and decimal units.

.. code-block:: bash

   pip install binary
`;

describe("SearchPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    api.fetchPackages.mockResolvedValue({
      data: [
        {
          ecosystem: "pypi",
          name: "Flask",
          normalized_name: "flask",
          description: "A microframework",
          license_spdx: "BSD-3-Clause",
          latest_version: "3.1.3",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    });
    api.searchPackages.mockResolvedValue({
      data: [],
      cursor: null,
      has_more: false,
    });
  });

  it("loads packages on first render", async () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );

    expect(await screen.findByRole("link", { name: "Flask" })).toBeInTheDocument();
    expect(screen.getByText("BSD-3-Clause")).toBeInTheDocument();
    expect(api.fetchPackages).toHaveBeenCalledWith({ limit: 20, sort: "name" });
  });

  it("runs search request when query is submitted", async () => {
    api.searchPackages.mockResolvedValue({
      data: [
        {
          ecosystem: "pypi",
          name: "Requests",
          normalized_name: "requests",
          description: "HTTP library",
          license_spdx: "Apache-2.0",
          latest_version: "2.32.5",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );

    const input = screen.getByPlaceholderText("flask, requests, numpy...");
    await user.clear(input);
    await user.type(input, "requests");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByRole("link", { name: "Requests" })).toBeInTheDocument();
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenCalledWith({ q: "requests", limit: 20, sort: "name" });
    });
  });

  it("shows autocomplete suggestions and navigates on suggestion click", async () => {
    api.searchPackages.mockResolvedValue({
      data: [
        {
          ecosystem: "pypi",
          name: "Requests",
          normalized_name: "requests",
          description: RAW_BINARY_DESCRIPTION,
          license_spdx: "Apache-2.0",
          latest_version: "2.32.5",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "pypi",
          name: "requests-oauthlib",
          normalized_name: "requests-oauthlib",
          description: "OAuth support for requests",
          license_spdx: "ISC",
          latest_version: "2.0.0",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/package/:ecosystem/*" element={<div>Package page</div>} />
        </Routes>
      </MemoryRouter>
    );

    const input = screen.getByLabelText("Search query");
    await user.clear(input);
    await user.type(input, "requests");

    expect(await screen.findByRole("listbox", { name: "Search suggestions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Requests/ })).toBeInTheDocument();
    expect(screen.getByText(/binary provides a bug-free and easy way to convert/i)).toBeInTheDocument();
    expect(screen.queryByText(/\.\. image::/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /requests-oauthlib/i }));

    expect(await screen.findByText("Package page")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenCalledWith({ q: "requests", limit: 8, sort: "name" });
    });
  });

  it("sends the chosen ecosystem to the list and search calls", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );
    await screen.findByRole("link", { name: "Flask" });

    await user.selectOptions(screen.getByLabelText("Ecosystem"), "npm");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", ecosystem: "npm" });
    });

    await user.type(screen.getByLabelText("Search query"), "left");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "left", limit: 20, sort: "name", ecosystem: "npm" });
    });

    await user.selectOptions(screen.getByLabelText("Ecosystem"), "");
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "left", limit: 20, sort: "name" });
    });
  });

  it("badges results by ecosystem and links npm packages by their scoped name", async () => {
    api.fetchPackages.mockResolvedValue({
      data: [
        {
          ecosystem: "npm",
          namespace: "angular",
          name: "core",
          normalized_name: "core",
          purl: "pkg:npm/%40angular/core",
          description: "Angular core",
          license_spdx: "MIT",
          latest_version: "19.0.0",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "npm",
          namespace: "",
          name: "left-pad",
          normalized_name: "left-pad",
          purl: "pkg:npm/left-pad",
          description: "Pad a string",
          license_spdx: "WTFPL",
          latest_version: "1.3.0",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "pypi",
          namespace: "",
          name: "Flask",
          normalized_name: "flask",
          purl: "pkg:pypi/flask",
          description: "A microframework",
          license_spdx: "BSD-3-Clause",
          latest_version: "3.1.3",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );

    const scoped = await screen.findByRole("link", { name: "@angular/core" });
    expect(scoped).toHaveAttribute("href", "/package/npm/@angular/core");
    expect(screen.getByRole("link", { name: "left-pad" })).toHaveAttribute("href", "/package/npm/left-pad");
    expect(screen.getByRole("link", { name: "Flask" })).toHaveAttribute("href", "/package/pypi/flask");
    expect(screen.getAllByText("npm", { selector: ".eco-badge" })).toHaveLength(2);
    expect(screen.getAllByText("PyPI", { selector: ".eco-badge" })).toHaveLength(1);
  });
});
