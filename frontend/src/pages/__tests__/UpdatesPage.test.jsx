import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import UpdatesPage from "../UpdatesPage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchUpdates: vi.fn(),
  fetchUpdatesSummary: vi.fn(),
}));

const SUMMARY = {
  since: "2026-10-20T00:00:00Z",
  until: "2026-10-27T00:00:00Z",
  last_run: {
    started_at: "2026-10-27T03:00:00Z",
    finished_at: "2026-10-27T03:12:40Z",
    status: "success",
    packages_updated: 140,
    packages_failed: 0,
  },
  totals: { packages: 412, releases: 655, yanked: 3, removed: 1, vulnerabilities: 2, unavailable: 0 },
  packages: [
    {
      ecosystem: "pypi",
      name: "flask",
      purl: "pkg:pypi/flask",
      from_version: "3.1.1",
      to_version: "3.2.0",
      max_bump: "major",
      releases: ["3.2.0rc1", "3.2.0"],
      yanked: ["3.1.0"],
      removed: [],
      vulnerabilities: ["GHSA-xxxx"],
      flags: ["vulnerability_added", "yanked"],
      last_detected_at: "2026-10-27T03:05:11Z",
    },
    {
      ecosystem: "pypi",
      name: "requests",
      purl: "pkg:pypi/requests",
      from_version: "2.32.4",
      to_version: "2.32.5",
      max_bump: "patch",
      releases: ["2.32.5"],
      yanked: [],
      removed: [],
      vulnerabilities: [],
      flags: [],
      last_detected_at: "2026-10-26T03:05:11Z",
    },
  ],
};

function renderPage() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <UpdatesPage />
    </MemoryRouter>
  );
}

describe("UpdatesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchUpdatesSummary.mockResolvedValue(SUMMARY);
    api.fetchUpdates.mockResolvedValue({
      data: [
        {
          id: 1,
          package: "flask",
          version: "3.2.0",
          kind: "release_added",
          detected_at: "2026-10-27T03:05:11Z",
          occurred_at: "2026-10-26T12:00:00Z",
          details: { previous_version: "3.1.1", bump: "major", prerelease: false },
        },
        {
          id: 2,
          package: "flask",
          version: "3.1.0",
          kind: "release_yanked",
          detected_at: "2026-10-27T03:05:12Z",
          occurred_at: null,
          details: {},
        },
      ],
      cursor: null,
      has_more: false,
    });
  });

  it("loads the daily summary with browser timezone on first render", async () => {
    renderPage();

    expect(await screen.findByRole("link", { name: "flask" })).toBeInTheDocument();
    expect(api.fetchUpdatesSummary).toHaveBeenCalledWith({
      period: "day",
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
  });

  it("renders last run, totals, versions and badges", async () => {
    renderPage();

    expect(await screen.findByRole("link", { name: "flask" })).toHaveAttribute("href", "/package/pypi/flask");
    expect(screen.getByText(/Data as of/)).toBeInTheDocument();

    const totals = screen.getByLabelText("Totals");
    expect(within(totals).getByText("Releases").nextSibling).toHaveTextContent("655");
    expect(within(totals).getByText("Packages").nextSibling).toHaveTextContent("412");

    expect(screen.getByText("major")).toBeInTheDocument();
    expect(screen.getByText("new vulnerability")).toBeInTheDocument();
    expect(screen.getByText("yanked")).toBeInTheDocument();
    expect(screen.getByText("3.1.1")).toBeInTheDocument();
    expect(screen.getByText("3.2.0")).toBeInTheDocument();
    expect(screen.getByText(/3\.2\.0rc1, 3\.2\.0/)).toBeInTheDocument();
    expect(screen.getByText(/GHSA-xxxx/)).toBeInTheDocument();
    expect(screen.getByText("patch")).toBeInTheDocument();
  });

  it("warns when the last run was not successful or had failures", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      last_run: { ...SUMMARY.last_run, status: "success", packages_failed: 3 },
    });
    renderPage();

    expect(await screen.findByText("3 failed")).toBeInTheDocument();
    expect(screen.getByText(/data may be incomplete/)).toBeInTheDocument();
  });

  it("shows no runs yet when last_run is null", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({ ...SUMMARY, last_run: null });
    renderPage();

    expect(await screen.findByText(/no runs yet/)).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      totals: { packages: 0, releases: 0, yanked: 0, removed: 0, vulnerabilities: 0, unavailable: 0 },
      packages: [],
    });
    renderPage();

    expect(await screen.findByText("No updates in this period.")).toBeInTheDocument();
  });

  it("shows an error when the request fails", async () => {
    api.fetchUpdatesSummary.mockRejectedValue({ code: "http_error", message: "HTTP 500" });
    renderPage();

    expect(await screen.findByText("HTTP 500")).toBeInTheDocument();
  });

  it("switches to week period", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("link", { name: "flask" });

    await user.click(screen.getByRole("button", { name: "Week" }));

    await waitFor(() => {
      expect(api.fetchUpdatesSummary).toHaveBeenLastCalledWith(
        expect.objectContaining({ period: "week" })
      );
    });
  });

  it("requests summary with since/until for a custom range", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("link", { name: "flask" });

    await user.click(screen.getByRole("button", { name: "Custom" }));
    await user.type(screen.getByLabelText("From"), "2026-10-20");
    await user.type(screen.getByLabelText("To"), "2026-10-26");

    await waitFor(() => {
      expect(api.fetchUpdatesSummary).toHaveBeenLastCalledWith({
        since: new Date(2026, 9, 20).toISOString(),
        until: new Date(2026, 9, 27).toISOString(),
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
    });
  });

  it("loads package events when a row is expanded", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("link", { name: "flask" });
    expect(api.fetchUpdates).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Show events for flask" }));

    expect(await screen.findByText("Release added")).toBeInTheDocument();
    expect(screen.getByText("Release yanked")).toBeInTheDocument();
    expect(api.fetchUpdates).toHaveBeenCalledWith(
      expect.objectContaining({
        package: "pkg:pypi/flask",
        since: SUMMARY.since,
        until: SUMMARY.until,
      })
    );
    expect(screen.getByText(/occurred/)).toBeInTheDocument();
  });
});

describe("UpdatesPage links by ecosystem", () => {
  it("links an npm entry to its npm package page", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [{ ...SUMMARY.packages[0], ecosystem: "npm", name: "core", purl: "pkg:npm/%40angular/core" }],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "@angular/core" });
    expect(link).toHaveAttribute("href", "/package/npm/%40angular/core");
  });

  it("links a crates entry to its crates package page", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [{ ...SUMMARY.packages[0], ecosystem: "crates", name: "Inflector", purl: "pkg:cargo/Inflector" }],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "Inflector" });
    expect(link).toHaveAttribute("href", "/package/crates/Inflector");
  });

  it("takes the ecosystem of an entry without one from its purl type", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [{ ...SUMMARY.packages[0], ecosystem: undefined, name: "serde_json", purl: "pkg:cargo/serde_json" }],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "serde_json" });
    expect(link).toHaveAttribute("href", "/package/crates/serde_json");
  });

  it("links a Go entry to its Go package page with the module path as is", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [
        {
          ...SUMMARY.packages[0],
          ecosystem: "go",
          name: "toml",
          purl: "pkg:golang/github.com/BurntSushi/toml",
        },
      ],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "github.com/BurntSushi/toml" });
    expect(link).toHaveAttribute("href", "/package/go/github.com/BurntSushi/toml");
  });

  it("takes the Go ecosystem of an entry without one from the golang purl type", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [
        { ...SUMMARY.packages[0], ecosystem: undefined, name: "go-redis", purl: "pkg:golang/github.com/redis/go-redis/v9" },
      ],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "github.com/redis/go-redis/v9" });
    expect(link).toHaveAttribute("href", "/package/go/github.com/redis/go-redis/v9");
  });

  it("links a NuGet entry to its NuGet package page by the lowercase id", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [
        { ...SUMMARY.packages[0], ecosystem: "nuget", name: "Newtonsoft.Json", purl: "pkg:nuget/newtonsoft.json" },
      ],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "newtonsoft.json" });
    expect(link).toHaveAttribute("href", "/package/nuget/newtonsoft.json");
  });

  it("takes the NuGet ecosystem of an entry without one from the nuget purl type", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [{ ...SUMMARY.packages[0], ecosystem: undefined, name: "Serilog", purl: "pkg:nuget/serilog.sinks.file" }],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "serilog.sinks.file" });
    expect(link).toHaveAttribute("href", "/package/nuget/serilog.sinks.file");
  });

  it("links a Maven entry to its package page by groupId:artifactId", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [
        {
          ...SUMMARY.packages[0],
          ecosystem: "maven",
          name: "org.springframework:spring-core",
          purl: "pkg:maven/org.springframework/spring-core",
        },
      ],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "org.springframework:spring-core" });
    expect(link).toHaveAttribute("href", "/package/maven/org.springframework:spring-core");
  });

  it("takes the Maven ecosystem of an entry without one from the maven purl type", async () => {
    api.fetchUpdatesSummary.mockResolvedValue({
      ...SUMMARY,
      packages: [
        { ...SUMMARY.packages[0], ecosystem: undefined, name: "guava", purl: "pkg:maven/com.google.guava/guava" },
      ],
    });
    renderPage();
    const link = await screen.findByRole("link", { name: "com.google.guava:guava" });
    expect(link).toHaveAttribute("href", "/package/maven/com.google.guava:guava");
  });
});
