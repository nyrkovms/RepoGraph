// The native name and package page of a digest entry: from its purl (`pkg:npm/%40angular/core` -> `@angular/core`),
// so an npm package does not link to a PyPI page of the same name.
export function displayName(item) {
  if (!item.purl) return item.name;
  // RPM: `pkg:rpm/rocky/openssl?distro=rocky-9` is the package `rocky-9/openssl`; the distro qualifier is the namespace
  // (the purl vendor, `rocky`, is not), so the same name in two releases stays two packages.
  if (item.purl.startsWith("pkg:rpm/")) {
    const distro = new URLSearchParams(item.purl.replace(/^[^?]*\??/, "").replace(/#.*$/, "")).get("distro");
    const name = decodeURIComponent(item.purl.replace(/[?#].*$/, "").replace(/@[^/]*$/, "").split("/").pop());
    return distro ? `${distro}/${name}` : item.name;
  }
  // The scope's `@` is encoded in a purl, so a literal `@` can only start a version.
  const native = decodeURIComponent(item.purl.replace(/^pkg:[^/]+\//, "").replace(/[?#].*$/, "").replace(/@[^/]*$/, ""));
  // Maven: `pkg:maven/org.springframework/spring-core` is the package `org.springframework:spring-core`.
  return item.purl.startsWith("pkg:maven/") ? native.replace("/", ":") : native;
}

// The slug of an ecosystem differs from its purl type for crates (`pkg:cargo/...`) and Go (`pkg:golang/...`).
const PURL_TYPE_ECOSYSTEMS = { cargo: "crates", golang: "go" };

export function ecosystemOf(item) {
  if (item.ecosystem) return item.ecosystem;
  const type = /^pkg:([^/]+)\//.exec(item.purl || "")?.[1];
  return PURL_TYPE_ECOSYSTEMS[type] || type || "pypi";
}

export function packageLink(item) {
  const ecosystem = ecosystemOf(item);
  return `/package/${ecosystem}/${displayName(item).split("/").map((part) => encodeURIComponent(part).replaceAll("%3A", ":")).join("/")}`;
}


export const FLAG_LABELS = {
  unavailable: "unavailable",
  vulnerability_added: "new vulnerability",
  removed: "removed",
  yanked: "yanked",
  deprecated: "deprecated",
  major: "major",
};


export function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString();
}

export function formatDateTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}


export function bumpOf(item) {
  return item.max_bump && item.max_bump !== "other" ? item.max_bump : null;
}

export function flagsOf(item) {
  const flags = Array.isArray(item.flags) ? [...item.flags] : [];
  if (item.max_bump === "major" && !flags.includes("major")) {
    flags.push("major");
  }
  return flags;
}

// `used by 9 packages · tracked directly`: how much of the catalog depends on the package, and why it is in it.
export function usageLine(item) {
  const parts = [];
  const usedBy = Number(item.used_by) || 0;
  if (usedBy > 0) parts.push(`used by ${usedBy} ${usedBy === 1 ? "package" : "packages"}`);
  if (item.origin === "manual" || item.origin === "seed") parts.push("tracked directly");
  else if (item.origin) parts.push("dependency");
  return parts.join(" · ");
}


// A daily run that finished more than 36 hours ago was missed: 24 hours plus slack for a long or late run.
export const STALE_RUN_HOURS = 36;

// Why a run counts as a problem: a list of short reasons, empty when the run is fine.
export function runProblems(run) {
  const reasons = [];
  if (run.status !== "success") reasons.push(run.status);
  const failed = run.packages_failed || 0;
  if (failed > 0) reasons.push(`${failed} failed`);
  const at = Date.parse(run.finished_at || run.started_at);
  const hours = (Date.now() - at) / 3_600_000;
  if (hours > STALE_RUN_HOURS) reasons.push(`no run for ${Math.round(hours / 24)} days`);
  return reasons;
}

// The latest finish among the runs and the runs that are a problem, as `{ name, reasons }`.
export function summarizeRuns(runs) {
  const asOf = runs.map((run) => run.finished_at || run.started_at).reduce((a, b) => (Date.parse(b) > Date.parse(a) ? b : a));
  const problems = runs
    .map((run) => ({ name: run.ecosystem || "last run", reasons: runProblems(run) }))
    .filter((p) => p.reasons.length);
  return { asOf, problems };
}

export function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
