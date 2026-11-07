import { installCommand } from "../installCommand";

// Shapes as the package API returns them: `name` as the registry spells it, `namespace` (npm scope without `@`,
// Maven groupId, RPM distro), `normalized_name` (local name; Go: the whole module path; RPM: no distro).
describe("installCommand", () => {
  it("pins the version for pip", () => {
    expect(installCommand("pypi", { name: "Flask", normalized_name: "flask", namespace: "" }, "3.1.3")).toBe("pip install Flask==3.1.3");
  });

  it("writes the scope of an npm package and none for an unscoped one", () => {
    expect(installCommand("npm", { name: "core", normalized_name: "core", namespace: "angular" }, "19.0.0")).toBe("npm i @angular/core@19.0.0");
    expect(installCommand("npm", { name: "left-pad", normalized_name: "left-pad", namespace: "" }, "1.3.0")).toBe("npm i left-pad@1.3.0");
    expect(installCommand("npm", { name: "@types/node", normalized_name: "node", namespace: "types" }, "22.0.0")).toBe("npm i @types/node@22.0.0");
  });

  it("uses the crate name as published", () => {
    expect(installCommand("crates", { name: "serde_json", normalized_name: "serde_json", namespace: "" }, "1.0.140")).toBe("cargo add serde_json@1.0.140");
  });

  it("uses the whole module path for Go, with the v of the version", () => {
    const module = { name: "github.com/BurntSushi/toml", normalized_name: "github.com/BurntSushi/toml", namespace: "" };
    expect(installCommand("go", module, "v1.4.0")).toBe("go get github.com/BurntSushi/toml@v1.4.0");
    expect(installCommand("go", { name: "toml", normalized_name: "toml", namespace: "github.com/BurntSushi" }, "v1.4.0")).toBe(
      "go get github.com/BurntSushi/toml@v1.4.0"
    );
    expect(installCommand("go", { name: "github.com/redis/go-redis/v9", normalized_name: "github.com/redis/go-redis/v9", namespace: "" }, "v9.5.1")).toBe(
      "go get github.com/redis/go-redis/v9@v9.5.1"
    );
  });

  it("keeps the registry's spelling of a NuGet id", () => {
    expect(installCommand("nuget", { name: "Newtonsoft.Json", normalized_name: "newtonsoft.json", namespace: "" }, "13.0.3")).toBe(
      "dotnet add package Newtonsoft.Json --version 13.0.3"
    );
  });

  it("gives a Maven coordinate groupId:artifactId:version", () => {
    expect(installCommand("maven", { name: "org.springframework:spring-core", normalized_name: "spring-core", namespace: "org.springframework" }, "6.1.14")).toBe(
      "org.springframework:spring-core:6.1.14"
    );
  });

  it("names an RPM without its distro and keeps the epoch of the version", () => {
    const rpm = { name: "openssl", normalized_name: "openssl", namespace: "rocky-9" };
    expect(installCommand("rpm", rpm, "1:3.0.7-27.el9")).toBe("dnf install openssl-1:3.0.7-27.el9");
    expect(installCommand("rpm", { ...rpm, normalized_name: "rocky-9/openssl" }, "3.0.7-27.el9")).toBe("dnf install openssl-3.0.7-27.el9");
  });

  it("gives nothing for an unknown ecosystem", () => {
    expect(installCommand("cobol", { name: "x" }, "1")).toBe("");
  });
});
