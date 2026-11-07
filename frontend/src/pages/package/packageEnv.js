export const ECOSYSTEM_LABELS = { pypi: "PyPI", npm: "npm", crates: "crates.io", go: "Go", nuget: "NuGet", maven: "Maven", rpm: "RPM" };

// npm environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): Node.js version, os and cpu as npm names them.
export const NPM_NODE_VERSIONS = ["22.0.0", "20.0.0", "18.0.0"];
export const NPM_OS_OPTIONS = [
  { value: "linux", label: "Linux" },
  { value: "darwin", label: "macOS" },
  { value: "win32", label: "Windows" },
];
export const NPM_CPU_OPTIONS = [
  { value: "x64", label: "x64" },
  { value: "arm64", label: "arm64" },
];
// crates environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): a Rust target triple the backend knows, features, default features.
export const CRATES_TARGETS = [
  "x86_64-unknown-linux-gnu",
  "x86_64-unknown-linux-musl",
  "aarch64-unknown-linux-gnu",
  "aarch64-unknown-linux-musl",
  "i686-unknown-linux-gnu",
  "armv7-unknown-linux-gnueabihf",
  "riscv64gc-unknown-linux-gnu",
  "powerpc64le-unknown-linux-gnu",
  "s390x-unknown-linux-gnu",
  "x86_64-pc-windows-msvc",
  "x86_64-pc-windows-gnu",
  "i686-pc-windows-msvc",
  "aarch64-pc-windows-msvc",
  "x86_64-apple-darwin",
  "aarch64-apple-darwin",
  "aarch64-apple-ios",
  "aarch64-linux-android",
  "x86_64-unknown-freebsd",
  "wasm32-unknown-unknown",
  "wasm32-wasip1",
  "wasm32-wasip2",
];
// NuGet environment (SPEC-MULTI-ECOSYSTEM.md section 7.3, Q7): the project's target framework, net5.0 and newer or
// netstandard1.0-2.1; any other value is a 422 of the API.
export const NUGET_FRAMEWORKS = [
  "net11.0",
  "net10.0",
  "net9.0",
  "net8.0",
  "net7.0",
  "net6.0",
  "net5.0",
  "netstandard2.1",
  "netstandard2.0",
  "netstandard1.6",
  "netstandard1.5",
  "netstandard1.4",
  "netstandard1.3",
  "netstandard1.2",
  "netstandard1.1",
  "netstandard1.0",
];
// Maven environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): the dependency scopes the tree follows; any other value is
// a 422 of the API. `compile,runtime` is what `mvn dependency:tree` shows for a runtime classpath.
export const MAVEN_SCOPES = ["compile", "runtime", "provided", "system", "test"];
// RPM environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): the architecture whose builds the tree follows; any other value
// is a 422 of the API.
export const RPM_ARCHES = ["x86_64", "aarch64", "ppc64le", "s390x"];
export const VERSIONS_SHOWN = 10;
// Release lines the Versions tab lists before `Show N older lines`.
export const LINES_SHOWN = 8;
export const DEFAULTS = {
  pypi: { python: "3.12", os: "linux" },
  npm: { node: "20.0.0", os: "linux", cpu: "x64" },
  crates: { target: "x86_64-unknown-linux-gnu" },
  nuget: { framework: "net8.0" },
  maven: { scopes: "compile,runtime" },
  rpm: { arch: "x86_64" },
};

export function pickOption(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

// "serde, std ,," -> "serde,std": the form of the `features` query parameter.
export function normalizeFeatures(text) {
  return String(text || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .join(",");
}

// "test, compile,bogus" -> "compile,test": the form of the `scopes` query parameter (known scopes, fixed order); the
// default when nothing valid is left.
export function normalizeScopes(text) {
  const wanted = new Set(String(text || "").split(",").map((item) => item.trim()));
  const scopes = MAVEN_SCOPES.filter((scope) => wanted.has(scope));
  return scopes.length ? scopes.join(",") : DEFAULTS.maven.scopes;
}

// Query of the dependency tree request: PyPI `python_version` and `os`, npm `node_version`, `os` and `cpu`,
// crates `target`, `features` and `default_features`, NuGet `target_framework`, Maven `scopes`, RPM `arch`, Go none.
export function treeParams(ecosystem, env, includeOptional) {
  if (ecosystem === "go") {
    return {};
  }
  if (ecosystem === "nuget") {
    return { target_framework: env.framework };
  }
  if (ecosystem === "maven") {
    return { scopes: env.scopes };
  }
  if (ecosystem === "rpm") {
    return { include_optional: includeOptional, arch: env.arch };
  }
  if (ecosystem === "npm") {
    return { include_optional: includeOptional, node_version: env.node, os: env.os, cpu: env.cpu };
  }
  if (ecosystem === "crates") {
    return { include_optional: includeOptional, target: env.target, features: env.features, default_features: env.defaultFeatures };
  }
  return { include_optional: includeOptional, python_version: env.python, os: env.os };
}

// The arch of an RPM artifact: the API's `arch` where it has one, otherwise the one the file name carries
// (`openssl-3.0.7-27.el9.x86_64.rpm`).
export function artifactArch(artifact) {
  if (artifact?.arch) {
    return String(artifact.arch);
  }
  return /\.([A-Za-z0-9_]+)\.rpm$/.exec(artifact?.filename || "")?.[1] || "";
}

// The arches of a version in a stable order: the four the catalog serves first, then the rest (noarch, src...).
export function archesOf(artifacts) {
  const arches = [...new Set((artifacts || []).map(artifactArch).filter(Boolean))];
  const rank = (arch) => (RPM_ARCHES.includes(arch) ? RPM_ARCHES.indexOf(arch) : RPM_ARCHES.length);
  return arches.sort((left, right) => rank(left) - rank(right) || left.localeCompare(right));
}
