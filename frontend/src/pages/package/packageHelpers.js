// Go has no tree environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): every `require` of a go.mod applies.
// A pseudo-version (`v0.0.0-20261014143641-f0251eb4aa3e`, `v1.2.4-0.2026...-abc`, `v1.2.4-pre.0.2026...-abc`) is a
// commit, not a release; `+incompatible` stays part of the version string.
export const GO_PSEUDO_VERSION = /^v\d+\.(?:0\.0-|\d+\.\d+-(?:[^+]*\.)?0\.)\d{14}-[0-9A-Za-z]+(?:\+[0-9A-Za-z.-]+)?$/;

export function isGoPseudoVersion(version) {
  return GO_PSEUDO_VERSION.test(String(version || ""));
}

export function formatMarkerLabel(condition) {
  const marker = condition?.marker;
  const op = condition?.op;
  const value = condition?.value;
  if (!marker || !op || !value) {
    return null;
  }

  if (marker === "python_version" || marker === "python_full_version") {
    return `Python ${op} ${value}`;
  }

  if (marker === "sys_platform") {
    return { linux: "Linux", win32: "Windows", darwin: "macOS" }[value] || value;
  }

  if (marker === "platform_system") {
    return value;
  }

  if (marker === "platform_machine") {
    return value;
  }

  if (marker === "os_name") {
    return null;
  }

  if (marker === "extra") {
    return `extra: ${value}`;
  }

  return null;
}

export function formatDateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

// Maven edge: the declared scope, exclusions, classifier and non-jar type. The scope is the edge's own when the API
// gives it; otherwise it follows from `dependency_type` where that is unambiguous (compile and runtime both map to
// `runtime`, so no label for them).
export function mavenEdgeLabels(node) {
  const labels = [];
  const scope = node.scope || node.details?.scope || { dev: "test", provided: "provided" }[node.dependency_type];
  if (scope) {
    labels.push(`scope ${scope}`);
  }
  const exclusions = Array.isArray(node.details?.exclusions) ? node.details.exclusions : [];
  if (exclusions.length) {
    labels.push(`excludes ${exclusions.map((item) => `${item.group}:${item.artifact}`).join(", ")}`);
  }
  if (node.details?.classifier) {
    labels.push(`classifier ${node.details.classifier}`);
  }
  if (node.details?.type) {
    labels.push(`type ${node.details.type}`);
  }
  return labels;
}

// RPM edge: the kind of dependency when it is not a hard `requires` (recommends, suggests, supplements, enhances) and
// the arches a dependency is limited to.
export function rpmEdgeLabels(node) {
  const labels = [];
  const kind = node.scope || node.details?.kind;
  if (kind && kind !== "requires") {
    labels.push(kind);
  }
  const arches = node.environment_markers?.arch;
  if (Array.isArray(arches) && arches.length) {
    labels.push(`arch ${arches.join(", ")}`);
  }
  return labels;
}

export function collectDependencyConditions(node, ecosystem) {
  const labels = [];
  const extraValues = new Set();
  const conditions = Array.isArray(node.environment_markers?.conditions) ? node.environment_markers.conditions : [];

  if (node.dependency_type === "optional") {
    labels.push("optional dependency");
  }

  if (node.dependency_type === "build") {
    labels.push("build dependency");
  }

  // crates: a platform-specific dependency carries the `cfg(...)` or target triple it is declared under.
  if (typeof node.environment_markers?.rust_cfg === "string") {
    labels.push(node.environment_markers.rust_cfg);
  }

  // Go: a requirement the module's go.mod marks `// indirect`.
  if (node.details?.indirect) {
    labels.push("indirect");
  }

  // NuGet: the dependency group (target framework, as the registration writes it) the edge is taken from.
  if (typeof node.environment_markers?.dotnet_target_framework === "string") {
    labels.push(node.environment_markers.dotnet_target_framework);
  }

  if (ecosystem === "maven") {
    labels.push(...mavenEdgeLabels(node));
  }

  if (ecosystem === "rpm") {
    labels.push(...rpmEdgeLabels(node));
  }

  if (node.extra_name) {
    extraValues.add(node.extra_name);
  }

  for (const condition of conditions) {
    if (condition.marker === "extra" && condition.value) {
      extraValues.add(condition.value);
      continue;
    }

    const label = formatMarkerLabel(condition);
    if (label) {
      labels.push(label);
    }
  }

  for (const extraValue of extraValues) {
    labels.push(`extra ${extraValue}`);
  }

  return {
    labels: [...new Set(labels)].sort((left, right) => {
      const leftExtra = left.startsWith("extra ");
      const rightExtra = right.startsWith("extra ");
      if (leftExtra !== rightExtra) {
        return leftExtra ? 1 : -1;
      }
      return left.localeCompare(right);
    }),
    extras: [...extraValues],
  };
}

// One date format for the whole UI, the one Updates uses.
export { formatDate } from "../../lib/updates";

export function isWebUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

// Deprecation of a package or version: {message, ...} (Go `// Deprecated:` in go.mod, npm, NuGet).
export function deprecationMessage(deprecation) {
  if (!deprecation) {
    return "";
  }
  return typeof deprecation === "string" ? deprecation : String(deprecation.message || "");
}

// Maven relocation: `deprecation.alternative` is `groupId:artifactId[:version]`, the package the POM moved to.
export function relocationTarget(alternative) {
  const [group, artifact, version] = String(alternative || "").split(":");
  if (!group || !artifact) {
    return null;
  }
  return { name: `${group}:${artifact}`, version: version || "" };
}

// Packaging of a Maven version (`jar`, `pom`, `aar`...), from its POM; the version payload carries it where it is known.
export function packagingOf(version) {
  const packaging = version?.packaging ?? version?.raw_metadata?.packaging;
  return typeof packaging === "string" ? packaging : "";
}

export function collectNodeIds(nodes, parentId = "root") {
  const ids = [];
  nodes.forEach((node, index) => {
    const nodeId = `${parentId}/${node.name}-${index}`;
    ids.push(nodeId);
    ids.push(...collectNodeIds(node.children || [], nodeId));
  });
  return ids;
}

// `?version=latest` is an alias: it starts empty and `loadPackage` settles on the package's latest version.
export function initialVersion(param) {
  return !param || param === "latest" ? "" : param;
}

// The advisory list is scoped to the package, not to a version (#68): an empty one is only a finding for the latest.
export function emptyVulnerabilitiesText(coverage, isLatest) {
  if (coverage !== "collected") {
    return "Not collected for this ecosystem";
  }
  return isLatest ? "No advisories found" : "Not checked for this version";
}

export const TABS = [
  { id: "overview", label: "Overview" },
  { id: "dependencies", label: "Dependencies" },
  { id: "used-by", label: "Used by" },
  { id: "versions", label: "Versions" },
  { id: "compare", label: "Compare" },
  { id: "sources", label: "Sources" },
];

// `?tab=`: absent or unknown means Overview.
export function readTab(value) {
  return TABS.some((tab) => tab.id === value) ? value : "overview";
}

// `?relation=` of the Used by and Dependencies tabs: absent or unknown means both.
export function readRelation(value) {
  return value === "direct" || value === "indirect" ? value : "";
}

// `?deps=` of the Dependencies tab: absent or unknown means the table.
export function readDepsView(value) {
  return value === "tree" ? "tree" : "";
}

// `?view=` of the Versions tab: absent or unknown means the release lines.
export function readVersionsView(value) {
  return value === "prereleases" || value === "not-active" || value === "deprecated" || value === "all" ? value : "lines";
}

// A catalog 404: the API's error object carries `status`; a rejected mock or an older client has only the message.
export function isNotFound(error) {
  return error?.status === 404 || error?.message === "HTTP 404";
}

// The purl of a version when the API has not given one: the version goes before the qualifiers (`?distro=...`).
export function versionedPurl(purl, version) {
  const [base, ...qualifiers] = String(purl || "").split("?");
  const suffix = qualifiers.length ? `?${qualifiers.join("?")}` : "";
  return `${base}@${encodeURIComponent(version).replaceAll("%3A", ":")}${suffix}`;
}

// "2026-01-01T00:00:00Z" -> "2026-01"; the activity chart's month labels.
export function monthLabel(month) {
  const [year, number] = String(month).split("-");
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${names[Number(number) - 1] || number} ${year}`;
}

export function plural(count, one, many = `${one}s`) {
  return `${count} ${count === 1 ? one : many}`;
}
