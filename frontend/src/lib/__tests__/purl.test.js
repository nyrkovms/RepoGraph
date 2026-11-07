import { isPurl, purlVersion } from "../purl";
import { displayName, packageHref } from "../packageNames";

describe("purlVersion", () => {
  it.each([
    ["pkg:npm/express@4.18.2", "4.18.2"],
    ["pkg:npm/%40angular/core@19.0.1", "19.0.1"],
    ["pkg:npm/@angular/core@19.0.1", "19.0.1"],      // a scope typed with a raw `@` is not a version
    ["pkg:npm/@angular/core", ""],
    ["pkg:pypi/django@5.1?extension=x#sub/path", "5.1"],
    ["pkg:golang/github.com/gin-gonic/gin@v1.10.0", "v1.10.0"],
    ["pkg:rpm/rocky/openssl@1%3A3.0.7-27.el9?distro=rocky-9", "1:3.0.7-27.el9"],
    ["pkg:maven/org.springframework/spring-core@6.1.0", "6.1.0"],
    ["pkg:npm/express", ""],
    ["pkg:npm/express?distro=x@y", ""],
    ["pkg:npm/express@%E0%A4%A", "%E0%A4%A"],        // not decodable: kept as typed
  ])("%s -> %j", (purl, version) => {
    expect(purlVersion(purl)).toBe(version);
  });
});

describe("isPurl", () => {
  it("recognises a purl by its scheme, whatever its case or padding", () => {
    expect(isPurl(" PKG:npm/x")).toBe(true);
    expect(isPurl("requests")).toBe(false);
    expect(isPurl("")).toBe(false);
    expect(isPurl(undefined)).toBe(false);
  });
});

describe("displayName", () => {
  it("does not repeat the scope the API already puts in the name of a scoped npm package", () => {
    const scoped = { ecosystem: "npm", namespace: "angular", name: "@angular/core", normalized_name: "core" };
    expect(displayName(scoped)).toBe("@angular/core");
    expect(packageHref(scoped)).toBe("/package/npm/@angular/core");
  });

  it("still prefixes a scope that is only in the namespace", () => {
    expect(displayName({ ecosystem: "npm", namespace: "angular", name: "core", normalized_name: "core" })).toBe("@angular/core");
  });

  it("leaves the other ecosystems' names as they are", () => {
    expect(displayName({ ecosystem: "npm", namespace: "", name: "left-pad", normalized_name: "left-pad" })).toBe("left-pad");
    expect(displayName({ ecosystem: "maven", namespace: "org.x", name: "org.x:lib", normalized_name: "lib" })).toBe("org.x:lib");
    expect(displayName({ ecosystem: "rpm", namespace: "rocky-9", name: "rocky-9/bash", normalized_name: "bash" })).toBe("rocky-9/bash");
  });
});
