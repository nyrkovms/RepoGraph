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
          <Route path="/package/:name" element={<div>Package page</div>} />
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

  it("passes facet filters to the catalog request", async () => {
    render(
      <MemoryRouter>
        <SearchPage />
      </MemoryRouter>
    );

    await waitFor(() => expect(api.fetchPackages).toHaveBeenCalled());
    api.fetchPackages.mockClear();

    await userEvent.selectOptions(screen.getByLabelText("Role filter"), "dev-tool");
    await waitFor(() =>
      expect(api.fetchPackages).toHaveBeenCalledWith(expect.objectContaining({ role: "dev-tool" }))
    );

    await userEvent.selectOptions(screen.getByLabelText("Status filter"), "needs-data");
    await waitFor(() =>
      expect(api.fetchPackages).toHaveBeenCalledWith(
        expect.objectContaining({ role: "dev-tool", status: "needs-data" })
      )
    );
  });

});
