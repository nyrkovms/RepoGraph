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

  it("builds the routes of a crate and passes the crates environment on the tree", async () => {
    await fetchPackage("crates", "serde_json");
    expect(lastUrl().pathname).toBe("/api/v1/packages/crates/serde_json/-/");

    await fetchVersion("crates", "Inflector", "0.11.4");
    expect(lastUrl().pathname).toBe("/api/v1/packages/crates/Inflector/-/versions/0.11.4");

    await fetchDependencies("crates", "serde_json", "1.0.140", {
      include_optional: false,
      target: "x86_64-pc-windows-msvc",
      features: "std,raw_value",
      default_features: false,
    });
    expect(lastUrl().pathname).toBe("/api/v1/packages/crates/serde_json/-/versions/1.0.140/dependencies");
    expect(Object.fromEntries(lastUrl().searchParams)).toEqual({
      include_optional: "false",
      target: "x86_64-pc-windows-msvc",
      features: "std,raw_value",
      default_features: "false",
    });

    // An empty features field is left out of the query.
    await fetchDependencies("crates", "serde_json", "1.0.140", { target: "wasm32-wasip1", features: "", default_features: true });
    expect(Object.fromEntries(lastUrl().searchParams)).toEqual({ target: "wasm32-wasip1", default_features: "true" });
  });

  it("builds the routes of a Go module: the slashes of the path stay, the case stays, versions are encoded", async () => {
    await fetchPackage("go", "github.com/BurntSushi/toml");
    expect(lastUrl().pathname).toBe("/api/v1/packages/go/github.com/BurntSushi/toml/-/");

    await fetchVersions("go", "github.com/redis/go-redis/v9", { limit: 30 });
    expect(lastUrl().pathname).toBe("/api/v1/packages/go/github.com/redis/go-redis/v9/-/versions");

    await fetchVersion("go", "github.com/gin-gonic/gin", "v2.0.0+incompatible");
    expect(lastUrl().pathname).toBe("/api/v1/packages/go/github.com/gin-gonic/gin/-/versions/v2.0.0%2Bincompatible");

    await fetchDependencies("go", "github.com/gin-gonic/gin", "v1.10.0", {});
    expect(lastUrl().pathname).toBe("/api/v1/packages/go/github.com/gin-gonic/gin/-/versions/v1.10.0/dependencies");
    expect(lastUrl().search).toBe("");
  });
});
