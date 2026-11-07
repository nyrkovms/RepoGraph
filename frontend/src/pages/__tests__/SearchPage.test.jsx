import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

import SearchPage from "../SearchPage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchCatalogSummary: vi.fn(),
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

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="search">{location.search}</output>;
}

function renderAt(url) {
  return render(
    <MemoryRouter initialEntries={[url]} future={FUTURE}>
      <SearchPage />
      <LocationProbe />
    </MemoryRouter>
  );
}

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
    api.fetchCatalogSummary.mockResolvedValue({
      licenses: [
        { license: "MIT", count: 133 },
        { license: "Apache-2.0", count: 90 },
      ],
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
      expect(api.searchPackages).toHaveBeenCalledWith({ q: "requests", limit: 20, sort: "relevance" });
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
      expect(api.searchPackages).toHaveBeenCalledWith({ q: "requests", limit: 8, sort: "relevance" });
    });
  });

  it("runs the search on Enter instead of opening the first suggestion", async () => {
    api.searchPackages.mockResolvedValue({
      data: [{ ecosystem: "pypi", name: "Requests", normalized_name: "requests", latest_version: "2.32.5" }],
      cursor: null,
      has_more: false,
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/search"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/search" element={<SearchPage />} />
          <Route path="/package/:ecosystem/*" element={<div>Package page</div>} />
        </Routes>
      </MemoryRouter>
    );
    await user.type(screen.getByLabelText("Search query"), "requests");
    await screen.findByRole("listbox", { name: "Search suggestions" });
    expect(document.querySelector(".autocomplete-item.active")).toBeNull();
    await user.keyboard("{Enter}");
    expect(screen.queryByText("Package page")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenCalledWith(expect.objectContaining({ q: "requests", limit: 20 }));
    });
  });

  it("opens the suggestion the reader moved onto with the arrows", async () => {
    api.searchPackages.mockResolvedValue({
      data: [{ ecosystem: "pypi", name: "Requests", normalized_name: "requests", latest_version: "2.32.5" }],
      cursor: null,
      has_more: false,
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/search"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/search" element={<SearchPage />} />
          <Route path="/package/:ecosystem/*" element={<div>Package page</div>} />
        </Routes>
      </MemoryRouter>
    );
    await user.type(screen.getByLabelText("Search query"), "requests");
    await screen.findByRole("listbox", { name: "Search suggestions" });
    await user.keyboard("{ArrowDown}{Enter}");
    expect(await screen.findByText("Package page")).toBeInTheDocument();
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
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "left", limit: 20, sort: "relevance", ecosystem: "npm" });
    });

    await user.selectOptions(screen.getByLabelText("Ecosystem"), "");
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "left", limit: 20, sort: "relevance" });
    });
  });

  it("badges results by ecosystem and links npm packages by their scoped name", async () => {
    api.fetchPackages.mockResolvedValue({
      data: [
        {
          ecosystem: "npm",
          namespace: "angular",
          name: "@angular/core",   // the real shape: the scope is in the name, the namespace has it without the `@`
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

  it("switches to crates.io and links crates by the name as published", async () => {
    const crates = {
      data: [
        {
          ecosystem: "crates",
          namespace: "",
          name: "serde_json",
          normalized_name: "serde_json",
          purl: "pkg:cargo/serde_json",
          description: "A JSON serialization file format",
          license_spdx: "MIT OR Apache-2.0",
          latest_version: "1.0.140",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "crates",
          namespace: "",
          name: "Inflector",
          normalized_name: "Inflector",
          purl: "pkg:cargo/Inflector",
          description: "String inflection",
          license_spdx: "BSD-2-Clause",
          latest_version: "0.11.4",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    };
    api.fetchPackages.mockResolvedValue(crates);
    api.searchPackages.mockResolvedValue(crates);
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );
    await screen.findByRole("link", { name: "serde_json" });

    expect(screen.getByRole("option", { name: "crates.io" })).toHaveValue("crates");
    await user.selectOptions(screen.getByLabelText("Ecosystem"), "crates");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", ecosystem: "crates" });
    });

    await user.type(screen.getByLabelText("Search query"), "serde");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "serde", limit: 20, sort: "relevance", ecosystem: "crates" });
    });

    expect(screen.getByRole("link", { name: "serde_json" })).toHaveAttribute("href", "/package/crates/serde_json");
    expect(screen.getByRole("link", { name: "Inflector" })).toHaveAttribute("href", "/package/crates/Inflector");
    expect(screen.getAllByText("crates.io", { selector: ".eco-badge" })).toHaveLength(2);
  });

  it("switches to Go and links modules by their full path", async () => {
    const modules = {
      data: [
        {
          ecosystem: "go",
          namespace: "",
          name: "github.com/gin-gonic/gin",
          normalized_name: "github.com/gin-gonic/gin",
          purl: "pkg:golang/github.com/gin-gonic/gin",
          description: null,
          license_spdx: null,
          latest_version: "v1.10.0",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "go",
          namespace: "",
          name: "github.com/BurntSushi/toml",
          normalized_name: "github.com/BurntSushi/toml",
          purl: "pkg:golang/github.com/BurntSushi/toml",
          description: null,
          license_spdx: null,
          latest_version: "v1.4.0",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "go",
          namespace: "",
          name: "github.com/redis/go-redis/v9",
          normalized_name: "github.com/redis/go-redis/v9",
          purl: "pkg:golang/github.com/redis/go-redis/v9",
          description: null,
          license_spdx: null,
          latest_version: "v9.7.0",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    };
    api.fetchPackages.mockResolvedValue(modules);
    api.searchPackages.mockResolvedValue(modules);
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );
    await screen.findByRole("link", { name: "github.com/gin-gonic/gin" });

    expect(screen.getByRole("option", { name: "Go" })).toHaveValue("go");
    await user.selectOptions(screen.getByLabelText("Ecosystem"), "go");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", ecosystem: "go" });
    });

    await user.type(screen.getByLabelText("Search query"), "gin");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "gin", limit: 20, sort: "relevance", ecosystem: "go" });
    });

    expect(screen.getByRole("link", { name: "github.com/gin-gonic/gin" })).toHaveAttribute(
      "href",
      "/package/go/github.com/gin-gonic/gin"
    );
    expect(screen.getByRole("link", { name: "github.com/BurntSushi/toml" })).toHaveAttribute(
      "href",
      "/package/go/github.com/BurntSushi/toml"
    );
    expect(screen.getByRole("link", { name: "github.com/redis/go-redis/v9" })).toHaveAttribute(
      "href",
      "/package/go/github.com/redis/go-redis/v9"
    );
    expect(screen.getAllByText("Go", { selector: ".eco-badge" })).toHaveLength(3);
  });

  it("switches to NuGet and links packages by their lowercase id, showing the registry's spelling", async () => {
    const packages = {
      data: [
        {
          ecosystem: "nuget",
          namespace: "",
          name: "Newtonsoft.Json",
          normalized_name: "newtonsoft.json",
          purl: "pkg:nuget/newtonsoft.json",
          description: "Json.NET",
          license_spdx: "MIT",
          latest_version: "13.0.3",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "nuget",
          namespace: "",
          name: "Serilog.Sinks.File",
          normalized_name: "serilog.sinks.file",
          purl: "pkg:nuget/serilog.sinks.file",
          description: null,
          license_spdx: "Apache-2.0",
          latest_version: "6.0.0",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    };
    api.fetchPackages.mockResolvedValue(packages);
    api.searchPackages.mockResolvedValue(packages);
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );
    await screen.findByRole("link", { name: "Newtonsoft.Json" });

    expect(screen.getByRole("option", { name: "NuGet" })).toHaveValue("nuget");
    await user.selectOptions(screen.getByLabelText("Ecosystem"), "nuget");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", ecosystem: "nuget" });
    });

    await user.type(screen.getByLabelText("Search query"), "json");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "json", limit: 20, sort: "relevance", ecosystem: "nuget" });
    });

    expect(screen.getByRole("link", { name: "Newtonsoft.Json" })).toHaveAttribute("href", "/package/nuget/newtonsoft.json");
    expect(screen.getByRole("link", { name: "Serilog.Sinks.File" })).toHaveAttribute(
      "href",
      "/package/nuget/serilog.sinks.file"
    );
    expect(screen.getAllByText("NuGet", { selector: ".eco-badge" })).toHaveLength(2);
  });

  it("switches to Maven, hints group:artifact and links artifacts by groupId:artifactId", async () => {
    const packages = {
      data: [
        {
          ecosystem: "maven",
          namespace: "org.springframework",
          name: "org.springframework:spring-core",
          normalized_name: "spring-core",
          purl: "pkg:maven/org.springframework/spring-core",
          description: "Spring Core",
          license_spdx: "Apache-2.0",
          latest_version: "6.1.14",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "maven",
          namespace: "com.google.guava",
          name: "com.google.guava:guava",
          normalized_name: "guava",
          purl: "pkg:maven/com.google.guava/guava",
          description: null,
          license_spdx: "Apache-2.0",
          latest_version: "33.3.1-jre",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    };
    api.fetchPackages.mockResolvedValue(packages);
    api.searchPackages.mockResolvedValue(packages);
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );
    await screen.findByRole("link", { name: "org.springframework:spring-core" });

    expect(screen.getByRole("option", { name: "Maven" })).toHaveValue("maven");
    expect(screen.getByLabelText("Search query")).toHaveAttribute("placeholder", "flask, requests, numpy...");
    await user.selectOptions(screen.getByLabelText("Ecosystem"), "maven");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", ecosystem: "maven" });
    });
    expect(screen.getByLabelText("Search query").getAttribute("placeholder")).toContain("group:artifact");

    await user.type(screen.getByLabelText("Search query"), "org.springframework:spring");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({
        q: "org.springframework:spring",
        limit: 20,
        sort: "relevance",
        ecosystem: "maven",
      });
    });

    expect(screen.getByRole("link", { name: "org.springframework:spring-core" })).toHaveAttribute(
      "href",
      "/package/maven/org.springframework:spring-core"
    );
    expect(screen.getByRole("link", { name: "com.google.guava:guava" })).toHaveAttribute(
      "href",
      "/package/maven/com.google.guava:guava"
    );
    expect(screen.getAllByText("Maven", { selector: ".eco-badge" })).toHaveLength(2);
  });

  it("switches to RPM, hints distro/name and links packages by distro/name", async () => {
    const packages = {
      data: [
        {
          ecosystem: "rpm",
          namespace: "rocky-9",
          name: "rocky-9/openssl",
          normalized_name: "openssl",
          purl: "pkg:rpm/rocky/openssl?distro=rocky-9",
          description: "Utilities from the general purpose cryptography library",
          license_spdx: null,
          latest_version: "1:3.0.7-27.el9",
          has_vulnerabilities: false,
        },
        {
          ecosystem: "rpm",
          namespace: "rhel-9",
          name: "rhel-9/openssl",
          normalized_name: "openssl",
          purl: "pkg:rpm/redhat/openssl?distro=rhel-9",
          description: null,
          license_spdx: null,
          latest_version: "1:3.0.7-28.el9_4",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    };
    api.fetchPackages.mockResolvedValue(packages);
    api.searchPackages.mockResolvedValue(packages);
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SearchPage />
      </MemoryRouter>
    );
    await screen.findByRole("link", { name: "rocky-9/openssl" });

    expect(screen.getByRole("option", { name: "RPM" })).toHaveValue("rpm");
    await user.selectOptions(screen.getByLabelText("Ecosystem"), "rpm");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", ecosystem: "rpm" });
    });
    expect(screen.getByLabelText("Search query").getAttribute("placeholder")).toContain("distro/name");
    expect(screen.getByLabelText("Search query").getAttribute("placeholder")).toContain("rocky-9/openssl");

    await user.type(screen.getByLabelText("Search query"), "rocky-9/openssl");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({
        q: "rocky-9/openssl",
        limit: 20,
        sort: "relevance",
        ecosystem: "rpm",
      });
    });

    // The same name in two releases is two packages with two pages.
    expect(screen.getByRole("link", { name: "rocky-9/openssl" })).toHaveAttribute("href", "/package/rpm/rocky-9/openssl");
    expect(screen.getByRole("link", { name: "rhel-9/openssl" })).toHaveAttribute("href", "/package/rpm/rhel-9/openssl");
    expect(screen.getAllByText("RPM", { selector: ".eco-badge" })).toHaveLength(2);
  });

  it("opens the RPM package page of a suggestion and puts distro/name in the field", async () => {
    const suggestion = {
      data: [
        {
          ecosystem: "rpm",
          namespace: "rocky-9",
          name: "rocky-9/openssl",
          normalized_name: "openssl",
          description: null,
          latest_version: "1:3.0.7-27.el9",
          has_vulnerabilities: false,
        },
      ],
      cursor: null,
      has_more: false,
    };
    api.searchPackages.mockResolvedValue(suggestion);
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/package/rpm/*" element={<p>rpm page</p>} />
        </Routes>
      </MemoryRouter>
    );
    await user.selectOptions(screen.getByLabelText("Ecosystem"), "rpm");
    await user.type(screen.getByLabelText("Search query"), "open");

    const list = await screen.findByRole("listbox", { name: "Search suggestions" });
    await user.click(within(list).getByRole("button", { name: /rocky-9\/openssl/ }));
    expect(await screen.findByText("rpm page")).toBeInTheDocument();
  });

  it("reads the query and filters from the address and searches with them", async () => {
    renderAt("/search?q=left&ecosystem=npm&sort=used_by&license=MIT&min_used_by=5&released_before=2023-11-07T00:00:00Z&deprecated=true");
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenCalledWith({
        q: "left",
        limit: 20,
        sort: "used_by",
        ecosystem: "npm",
        license: "MIT",
        min_used_by: "5",
        released_before: "2023-11-07T00:00:00Z",
        deprecated: true,
      });
    });
    expect(api.fetchPackages).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Search query")).toHaveValue("left");
    expect(screen.getByLabelText("Sort")).toHaveValue("used_by");
    expect(screen.getByLabelText("License")).toHaveValue("MIT");
  });

  it("asks for no suggestions for the query already showing as results", async () => {
    const user = userEvent.setup();
    api.searchPackages.mockResolvedValue(await api.fetchPackages());
    api.fetchPackages.mockClear();
    renderAt("/search?q=flask");
    await screen.findByRole("link", { name: "Flask" });
    await new Promise((resolve) => window.setTimeout(resolve, 300));
    expect(api.searchPackages).toHaveBeenCalledTimes(1);

    await user.type(screen.getByLabelText("Search query"), "x");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith(expect.objectContaining({ q: "flaskx", limit: 20 }));
    });
    await new Promise((resolve) => window.setTimeout(resolve, 300));
    expect(api.searchPackages).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("listbox", { name: "Search suggestions" })).not.toBeInTheDocument();
  });

  it("writes sort and license to the address and reloads", async () => {
    const user = userEvent.setup();
    renderAt("/search");
    await screen.findByRole("link", { name: "Flask" });
    // Relevance ranks the matches of a query: there is none yet.
    expect(screen.queryByRole("option", { name: "Relevance" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "All licenses" })).toHaveValue("");

    await user.selectOptions(screen.getByLabelText("Sort"), "used_by");
    expect(screen.getByTestId("search")).toHaveTextContent("?sort=used_by");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "used_by" });
    });

    await user.selectOptions(await screen.findByRole("option", { name: "Apache-2.0" }).then((o) => o.parentElement), "Apache-2.0");
    expect(screen.getByTestId("search").textContent).toContain("license=Apache-2.0");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "used_by", license: "Apache-2.0" });
    });

    await user.type(screen.getByLabelText("Search query"), "flask");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByTestId("search").textContent).toContain("q=flask");
    expect(await screen.findByRole("option", { name: "Relevance" })).toBeInTheDocument();
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "flask", limit: 20, sort: "used_by", license: "Apache-2.0" });
    });

    await user.selectOptions(screen.getByLabelText("Sort"), "relevance");
    expect(screen.getByTestId("search").textContent).not.toContain("sort=");
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenLastCalledWith({ q: "flask", limit: 20, sort: "relevance", license: "Apache-2.0" });
    });
  });

  it("defaults to relevance with a query", async () => {
    renderAt("/search?q=flask");
    await waitFor(() => {
      expect(api.searchPackages).toHaveBeenCalledWith({ q: "flask", limit: 20, sort: "relevance" });
    });
    expect(screen.getByLabelText("Sort")).toHaveValue("relevance");
  });

  it("falls back to the default sort when relevance is asked for without a query", async () => {
    renderAt("/search?sort=relevance");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenCalledWith({ limit: 20, sort: "name" });
    });
  });

  it("shows a chip for each filter without a control and clears it", async () => {
    const user = userEvent.setup();
    renderAt("/search?min_used_by=5&released_before=2023-11-07T00:00:00Z&deprecated=true&sort=used_by");
    const chips = await screen.findByRole("list", { name: "Active filters" });
    expect(within(chips).getByText("used by 5+")).toBeInTheDocument();
    expect(within(chips).getByText(/latest release before/)).toBeInTheDocument();
    expect(within(chips).getByText("deprecated")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filter: used by 5+" }));
    expect(screen.getByTestId("search").textContent).not.toContain("min_used_by");
    expect(screen.getByTestId("search").textContent).toContain("deprecated=true");
    await waitFor(() => {
      expect(api.fetchPackages).toHaveBeenLastCalledWith({
        limit: 20,
        sort: "used_by",
        released_before: "2023-11-07T00:00:00Z",
        deprecated: true,
      });
    });

    await user.click(screen.getByRole("button", { name: "Clear filter: deprecated" }));
    await user.click(screen.getByRole("button", { name: /Clear filter: latest release before/ }));
    expect(screen.queryByRole("list", { name: "Active filters" })).not.toBeInTheDocument();
    expect(screen.getByTestId("search")).toHaveTextContent("?sort=used_by");
  });

  it("shows used by in a row only when someone uses the package", async () => {
    api.fetchPackages.mockResolvedValue({
      data: [
        { ecosystem: "npm", namespace: "", name: "left-pad", normalized_name: "left-pad", latest_version: "1.3.0", used_by: 12 },
        { ecosystem: "npm", namespace: "", name: "lonely", normalized_name: "lonely", latest_version: "1.0.0", used_by: 0 },
      ],
      cursor: null,
      has_more: false,
    });
    renderAt("/search");
    await screen.findByRole("link", { name: "left-pad" });
    expect(screen.getAllByText(/^used by \d+$/)).toHaveLength(1);
    expect(screen.getByText("used by 12")).toBeInTheDocument();
  });

  it("explains the used by number once under the list, and not when no row has one", async () => {
    api.fetchPackages.mockResolvedValue({
      data: [
        { ecosystem: "npm", namespace: "", name: "left-pad", normalized_name: "left-pad", latest_version: "1.3.0", used_by: 12 },
        { ecosystem: "npm", namespace: "", name: "right-pad", normalized_name: "right-pad", latest_version: "1.0.0", used_by: 3 },
      ],
      cursor: null,
      has_more: false,
    });
    renderAt("/search");
    await screen.findByRole("link", { name: "left-pad" });
    expect(screen.getAllByText("used by counts packages in this catalog")).toHaveLength(1);
  });

  it("has no caption when no row shows a used by number", async () => {
    renderAt("/search");
    await screen.findByRole("link", { name: "Flask" });
    expect(screen.queryByText("used by counts packages in this catalog")).not.toBeInTheDocument();
  });

  it("shows no description line for a package without a description", async () => {
    api.fetchPackages.mockResolvedValue({
      data: [
        { ecosystem: "go", namespace: "", name: "github.com/a/b", normalized_name: "github.com/a/b", description: null, latest_version: "v1.0.0" },
        { ecosystem: "go", namespace: "", name: "github.com/c/d", normalized_name: "github.com/c/d", description: "", latest_version: "v1.0.0" },
      ],
      cursor: null,
      has_more: false,
    });
    renderAt("/search");
    const row = (await screen.findByRole("link", { name: "github.com/a/b" })).closest("li");
    expect(row.querySelector("p")).toBeNull();
    expect(screen.queryByText("No description")).not.toBeInTheDocument();
    expect(screen.queryByText("n/a")).not.toBeInTheDocument();
  });

  it("loads the next page with the cursor", async () => {
    const user = userEvent.setup();
    api.fetchPackages.mockResolvedValueOnce({
      data: [{ ecosystem: "pypi", namespace: "", name: "a", normalized_name: "a", latest_version: "1" }],
      cursor: "next-page",
      has_more: true,
    });
    api.fetchPackages.mockResolvedValueOnce({
      data: [{ ecosystem: "pypi", namespace: "", name: "b", normalized_name: "b", latest_version: "1" }],
      cursor: null,
      has_more: false,
    });
    renderAt("/search");
    await screen.findByRole("link", { name: "a" });
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("link", { name: "b" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "a" })).toBeInTheDocument();
    expect(api.fetchPackages).toHaveBeenLastCalledWith({ limit: 20, sort: "name", cursor: "next-page" });
  });

  it("says what was searched when nothing matches, and names a purl", async () => {
    renderAt("/search?q=nothing-like-this");
    expect(await screen.findByText('No packages match "nothing-like-this"')).toBeInTheDocument();
    expect(screen.queryByText("No package matches this purl")).not.toBeInTheDocument();
  });

  it("explains an empty purl search", async () => {
    renderAt("/search?q=pkg:npm/missing");
    expect(await screen.findByText('No packages match "pkg:npm/missing"')).toBeInTheDocument();
    expect(screen.getByText("No package matches this purl")).toBeInTheDocument();
  });

  describe("a purl typed in the search box", () => {
    function renderWithCard() {
      return render(
        <MemoryRouter initialEntries={["/search"]} future={FUTURE}>
          <LocationProbe />
          <Routes>
            <Route path="/search" element={<SearchPage />} />
            <Route path="*" element={<p>The card</p>} />
          </Routes>
        </MemoryRouter>
      );
    }
    const where = () => screen.getByTestId("search").textContent;
    const express = { ecosystem: "npm", namespace: "", name: "express", normalized_name: "express", purl: "pkg:npm/express", latest_version: "4.19.2" };

    it("opens the card when exactly one package matches", async () => {
      const user = userEvent.setup();
      api.searchPackages.mockResolvedValue({ data: [express], cursor: null, has_more: false });
      renderWithCard();
      await user.type(screen.getByLabelText("Search query"), "pkg:npm/express{Enter}");
      expect(await screen.findByText("The card")).toBeInTheDocument();
      expect(where()).toBe("");
      expect(api.searchPackages).toHaveBeenCalledWith({ q: "pkg:npm/express", limit: 2 });
    });

    it("carries the version of the purl to ?version=", async () => {
      const user = userEvent.setup();
      api.searchPackages.mockResolvedValue({ data: [{ ...express, namespace: "angular", name: "@angular/core", normalized_name: "core" }], cursor: null, has_more: false });
      renderWithCard();
      await user.type(screen.getByLabelText("Search query"), "pkg:npm/%40angular/core@19.0.1{Enter}");
      expect(await screen.findByText("The card")).toBeInTheDocument();
      expect(where()).toBe("?version=19.0.1");
    });

    it("stays on the results when the purl matches none or several", async () => {
      const user = userEvent.setup();
      renderWithCard();
      await user.type(screen.getByLabelText("Search query"), "pkg:npm/nope{Enter}");
      await waitFor(() => expect(where()).toBe("?q=pkg%3Anpm%2Fnope"));
      expect(screen.getByRole("heading", { name: "Search packages" })).toBeInTheDocument();

      api.searchPackages.mockResolvedValue({ data: [express, { ...express, ecosystem: "pypi", purl: "pkg:pypi/express" }], cursor: null, has_more: false });
      await user.clear(screen.getByLabelText("Search query"));
      await user.type(screen.getByLabelText("Search query"), "pkg:npm/express{Enter}");
      await waitFor(() => expect(where()).toBe("?q=pkg%3Anpm%2Fexpress"));
      expect(screen.getByRole("heading", { name: "Search packages" })).toBeInTheDocument();
    });
  });

  it("still lists packages when the license list cannot be loaded", async () => {
    api.fetchCatalogSummary.mockRejectedValue({ message: "HTTP 500" });
    renderAt("/search?license=MIT");
    await waitFor(() => expect(api.fetchPackages).toHaveBeenCalled());
    // The chosen license stays visible in the select.
    expect(screen.getByLabelText("License")).toHaveValue("MIT");
  });
});
