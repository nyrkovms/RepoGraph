import { fetchDependencies, fetchPackage, fetchVersion, fetchVersions, fetchVulnerabilities } from "../../lib/api";

describe("package routes", () => {
  let fetchMock;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => "{}" });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function lastUrl() {
    return new URL(fetchMock.mock.calls.at(-1)[0]);
  }

  it("builds the purl route of a scoped npm package and keeps the slash between scope and name", async () => {
    await fetchPackage("npm", "@angular/core");
    expect(lastUrl().pathname + lastUrl().search).toBe("/api/v1/packages/npm/%40angular/core/-/");

    await fetchVersions("npm", "@angular/core", { limit: 30 });
    expect(lastUrl().pathname + lastUrl().search).toBe("/api/v1/packages/npm/%40angular/core/-/versions?limit=30");

    await fetchVersion("npm", "@angular/core", "19.0.0-rc.1+build");
    expect(lastUrl().pathname).toBe("/api/v1/packages/npm/%40angular/core/-/versions/19.0.0-rc.1%2Bbuild");

    await fetchDependencies("npm", "@angular/core", "19.0.0", { include_optional: false, os: "linux" });
    expect(lastUrl().pathname + lastUrl().search).toBe(
      "/api/v1/packages/npm/%40angular/core/-/versions/19.0.0/dependencies?include_optional=false&os=linux"
    );

    await fetchVulnerabilities("npm", "@angular/core");
    expect(lastUrl().pathname).toBe("/api/v1/packages/npm/%40angular/core/-/vulnerabilities");
  });

  it("uses the same routes for unscoped npm and PyPI names", async () => {
    await fetchPackage("npm", "left-pad");
    expect(lastUrl().pathname).toBe("/api/v1/packages/npm/left-pad/-/");

    await fetchPackage("pypi", "flask");
    expect(lastUrl().pathname).toBe("/api/v1/packages/pypi/flask/-/");
  });
});
