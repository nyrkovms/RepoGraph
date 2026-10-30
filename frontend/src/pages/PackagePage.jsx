import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { fetchDependencies, fetchPackage, fetchVersion, fetchVersions, fetchVulnerabilities } from "../lib/api";
import { getPackageDescriptionParagraphs } from "../lib/packageDescription";
import { OS_OPTIONS, PYTHON_VERSIONS } from "../lib/runtimeEnv";

const ECOSYSTEM_LABELS = { pypi: "PyPI", npm: "npm", crates: "crates.io", go: "Go" };

// npm environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): Node.js version, os and cpu as npm names them.
const NPM_NODE_VERSIONS = ["22.0.0", "20.0.0", "18.0.0"];
const NPM_OS_OPTIONS = [
  { value: "linux", label: "Linux" },
  { value: "darwin", label: "macOS" },
  { value: "win32", label: "Windows" },
];
const NPM_CPU_OPTIONS = [
  { value: "x64", label: "x64" },
  { value: "arm64", label: "arm64" },
];
// crates environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): a Rust target triple the backend knows, features, default features.
const CRATES_TARGETS = [
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
const DEFAULTS = {
  pypi: { python: "3.12", os: "linux" },
  npm: { node: "20.0.0", os: "linux", cpu: "x64" },
  crates: { target: "x86_64-unknown-linux-gnu" },
};
// Go has no tree environment (SPEC-MULTI-ECOSYSTEM.md section 7.3): every `require` of a go.mod applies.
// A pseudo-version (`v0.0.0-20261014143641-f0251eb4aa3e`, `v1.2.4-0.2026...-abc`, `v1.2.4-pre.0.2026...-abc`) is a
// commit, not a release; `+incompatible` stays part of the version string.
const GO_PSEUDO_VERSION = /^v\d+\.(?:0\.0-|\d+\.\d+-(?:[^+]*\.)?0\.)\d{14}-[0-9A-Za-z]+(?:\+[0-9A-Za-z.-]+)?$/;

function isGoPseudoVersion(version) {
  return GO_PSEUDO_VERSION.test(String(version || ""));
}

// Internal key of the version's feature map, not a feature.
const HIDDEN_FEATURE_KEYS = new Set(["dependencies_hydrated"]);

function pickOption(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

// "serde, std ,," -> "serde,std": the form of the `features` query parameter.
function normalizeFeatures(text) {
  return String(text || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .join(",");
}

// Query of the dependency tree request: PyPI `python_version` and `os`, npm `node_version`, `os` and `cpu`,
// crates `target`, `features` and `default_features`, Go none.
function treeParams(ecosystem, env, includeOptional) {
  if (ecosystem === "go") {
    return {};
  }
  if (ecosystem === "npm") {
    return { include_optional: includeOptional, node_version: env.node, os: env.os, cpu: env.cpu };
  }
  if (ecosystem === "crates") {
    return { include_optional: includeOptional, target: env.target, features: env.features, default_features: env.defaultFeatures };
  }
  return { include_optional: includeOptional, python_version: env.python, os: env.os };
}

function formatMarkerLabel(condition) {
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

function formatDateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function collectDependencyConditions(node) {
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

function formatDate(value) {
  const match = /^\d{4}-\d{2}-\d{2}/.exec(String(value || ""));
  return match ? match[0] : "";
}

function isWebUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

function UrlValue({ url }) {
  if (!url) {
    return "n/a";
  }
  return isWebUrl(url) ? (
    <a href={url} target="_blank" rel="noreferrer noopener">
      {url}
    </a>
  ) : (
    url
  );
}

// Cargo feature map of a version: feature name -> what it enables (other features, `dep:crate`, `crate/feature`).
function featureEntries(features) {
  if (!features || typeof features !== "object") {
    return [];
  }
  return Object.entries(features)
    .filter(([name]) => !HIDDEN_FEATURE_KEYS.has(name))
    .sort(([left], [right]) => Number(right === "default") - Number(left === "default") || left.localeCompare(right));
}

function FeaturesBlock({ version, features }) {
  const entries = featureEntries(features);
  return (
    <div className="version-features">
      <h3>Features of {version}</h3>
      {entries.length === 0 ? <p className="muted">No features.</p> : null}
      {entries.length ? (
        <dl className="feature-list">
          {entries.map(([name, enables]) => {
            const list = Array.isArray(enables) ? enables : [];
            return (
              <div key={name} className="feature-item">
                <dt>
                  <code>{name}</code>
                </dt>
                <dd>{list.length ? list.map((item) => <code key={item}>{item}</code>) : <span className="muted">enables nothing</span>}</dd>
              </div>
            );
          })}
        </dl>
      ) : null}
    </div>
  );
}

// Module-level deprecation (`// Deprecated:` in go.mod) of a package or version: {message, ...}.
function deprecationMessage(deprecation) {
  if (!deprecation) {
    return "";
  }
  return typeof deprecation === "string" ? deprecation : String(deprecation.message || "");
}

function VersionStatus({ version, isGo }) {
  // A Go module's author retracts a version in go.mod with a rationale; the catalog keeps it as yanked.
  const retracted = isGo && version.status === "yanked";
  return (
    <>
      <span className={version.status === "yanked" ? "tag warn" : "tag"}>{retracted ? "retracted" : version.status}</span>
      {isGoPseudoVersion(version.version) ? <span className="tag cond">pseudo-version</span> : null}
      {retracted && version.status_reason ? <span className="muted version-reason">{version.status_reason}</span> : null}
    </>
  );
}

function collectNodeIds(nodes, parentId = "root") {
  const ids = [];
  nodes.forEach((node, index) => {
    const nodeId = `${parentId}/${node.name}-${index}`;
    ids.push(nodeId);
    ids.push(...collectNodeIds(node.children || [], nodeId));
  });
  return ids;
}

function DependencyTreeNode({ node, nodeId, packageName, collapsedNodes, onToggleCollapse }) {
  const conditions = collectDependencyConditions(node);
  const installHints = conditions.extras.map((extra) => `${packageName}[${extra}]`);
  const hasChildren = Boolean(node.children?.length);
  const isCollapsed = collapsedNodes.has(nodeId);

  return (
    <li className="dep-tree-item">
      <div className="dep-node">
        <div className="dep-node-main">
          {hasChildren ? (
            <button
              type="button"
              className="tree-toggle"
              onClick={() => onToggleCollapse(nodeId)}
              aria-label={isCollapsed ? `Expand ${node.name}` : `Collapse ${node.name}`}
            >
              {isCollapsed ? "+" : "-"}
            </button>
          ) : (
            <span className="tree-toggle-spacer" aria-hidden="true" />
          )}
          <code className="dep-node-name">{node.name}</code>
          {node.constraint && node.constraint !== "*" ? <span className="muted dep-constraint">{node.constraint}</span> : null}
          {node.resolved_version ? <span className="tag dep-version-tag">{node.resolved_version}</span> : <span className="tag muted-tag dep-version-tag">unresolved</span>}
          {node.cycle ? <span className="tag warn">cycle</span> : null}
          {node.dependencies_unknown ? <span className="tag warn">dependencies unknown</span> : null}
        </div>
        {conditions.labels.length ? (
          <div className="dep-node-meta">
            {conditions.labels.map((label) => (
              <span key={label} className="tag cond">
                {label}
              </span>
            ))}
          </div>
        ) : null}
        {installHints.length ? (
          <p className="muted dep-node-hint">
            Enable with <code>{installHints.join(" ")}</code>
          </p>
        ) : null}
      </div>
      {!isCollapsed ? (
        <DependencyTree
          nodes={node.children}
          packageName={packageName}
          parentId={nodeId}
          collapsedNodes={collapsedNodes}
          onToggleCollapse={onToggleCollapse}
        />
      ) : null}
    </li>
  );
}

function DependencyTree({ nodes, packageName, parentId = "root", collapsedNodes, onToggleCollapse }) {
  if (!nodes?.length) {
    return null;
  }

  return (
    <ul className="dep-tree">
      {nodes.map((node, idx) => (
        <DependencyTreeNode
          key={`${node.name}-${idx}`}
          node={node}
          nodeId={`${parentId}/${node.name}-${idx}`}
          packageName={packageName}
          collapsedNodes={collapsedNodes}
          onToggleCollapse={onToggleCollapse}
        />
      ))}
    </ul>
  );
}

export default function PackagePage() {
  // `/package/:ecosystem/*` carries native coordinates (`@angular/core`); `/package/:name` is the old PyPI route.
  const params = useParams();
  const ecosystem = params.ecosystem || "pypi";
  const name = params.ecosystem ? params["*"] : params.name;
  const isNpm = ecosystem === "npm";
  const isCrates = ecosystem === "crates";
  const isGo = ecosystem === "go";
  const [searchParams, setSearchParams] = useSearchParams();

  const [pkg, setPkg] = useState(null);
  const [versions, setVersions] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState(() => searchParams.get("version") || "");
  const [pythonVersion, setPythonVersion] = useState(() => searchParams.get("python") || DEFAULTS.pypi.python);
  const [nodeVersion, setNodeVersion] = useState(() => searchParams.get("node") || DEFAULTS.npm.node);
  const [cpu, setCpu] = useState(() => searchParams.get("cpu") || DEFAULTS.npm.cpu);
  const [os, setOs] = useState(() => searchParams.get("os") || DEFAULTS.pypi.os);
  const [target, setTarget] = useState(() => searchParams.get("target") || DEFAULTS.crates.target);
  // The features field is applied (and the tree reloaded) on Enter or blur, not on every keystroke.
  const [features, setFeatures] = useState(() => normalizeFeatures(searchParams.get("features")));
  const [featuresInput, setFeaturesInput] = useState(() => normalizeFeatures(searchParams.get("features")));
  const [defaultFeatures, setDefaultFeatures] = useState(() => searchParams.get("default_features") !== "0");
  const [vulns, setVulns] = useState([]);
  const [deps, setDeps] = useState([]);
  const [depsUnknown, setDepsUnknown] = useState(false);
  const [versionDetail, setVersionDetail] = useState(null);
  const [includeOptional, setIncludeOptional] = useState(() => searchParams.get("include_optional") === "1");
  const [loading, setLoading] = useState(false);
  const [depsLoading, setDepsLoading] = useState(false);
  const [error, setError] = useState("");
  const [collapsedNodes, setCollapsedNodes] = useState(() => new Set());

  // `os` is one URL parameter for both ecosystems, but PyPI calls Windows "windows" and npm "win32".
  const treeOs = isNpm ? pickOption(os, NPM_OS_OPTIONS.map((item) => item.value), DEFAULTS.npm.os) : os;
  const treeTarget = pickOption(target, CRATES_TARGETS, DEFAULTS.crates.target);
  const latestVersion = useMemo(() => selectedVersion || pkg?.latest_version || versions?.[0]?.version, [selectedVersion, pkg, versions]);
  // `deprecation.message` of the package (Go: the `// Deprecated:` paragraph of go.mod), `true` when it has no text.
  const deprecated = useMemo(() => {
    if (!pkg?.deprecation) {
      return "";
    }
    return deprecationMessage(pkg.deprecation) || true;
  }, [pkg?.deprecation]);
  const descriptionParagraphs = useMemo(() => getPackageDescriptionParagraphs(pkg?.description), [pkg?.description]);
  const allNodeIds = useMemo(() => collectNodeIds(deps), [deps]);
  const isTreeCollapsed = useMemo(
    () => allNodeIds.length > 0 && allNodeIds.every((nodeId) => collapsedNodes.has(nodeId)),
    [allNodeIds, collapsedNodes]
  );

  useEffect(() => {
    if (!name) {
      return;
    }
    setSelectedVersion(searchParams.get("version") || "");
    setPythonVersion(searchParams.get("python") || DEFAULTS.pypi.python);
    setNodeVersion(searchParams.get("node") || DEFAULTS.npm.node);
    setCpu(searchParams.get("cpu") || DEFAULTS.npm.cpu);
    setOs(searchParams.get("os") || DEFAULTS.pypi.os);
    setTarget(searchParams.get("target") || DEFAULTS.crates.target);
    setFeatures(normalizeFeatures(searchParams.get("features")));
    setFeaturesInput(normalizeFeatures(searchParams.get("features")));
    setDefaultFeatures(searchParams.get("default_features") !== "0");
    setIncludeOptional(searchParams.get("include_optional") === "1");
    setDeps([]);
    setDepsUnknown(false);
    setVersionDetail(null);
    setCollapsedNodes(new Set());
    loadPackage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ecosystem, name]);

  useEffect(() => {
    const next = new URLSearchParams();
    if (selectedVersion) {
      next.set("version", selectedVersion);
    }
    if (isNpm) {
      if (nodeVersion && nodeVersion !== DEFAULTS.npm.node) {
        next.set("node", nodeVersion);
      }
      if (cpu && cpu !== DEFAULTS.npm.cpu) {
        next.set("cpu", cpu);
      }
    } else if (isCrates) {
      if (target && target !== DEFAULTS.crates.target) {
        next.set("target", target);
      }
      if (features) {
        next.set("features", features);
      }
      if (!defaultFeatures) {
        next.set("default_features", "0");
      }
    } else if (!isGo && pythonVersion && pythonVersion !== DEFAULTS.pypi.python) {
      next.set("python", pythonVersion);
    }
    if (!isCrates && !isGo && os && os !== DEFAULTS.pypi.os) {
      next.set("os", os);
    }
    if (includeOptional && !isGo) {
      next.set("include_optional", "1");
    }
    setSearchParams(next, { replace: true });
  }, [isNpm, isCrates, isGo, selectedVersion, pythonVersion, nodeVersion, cpu, os, target, features, defaultFeatures, includeOptional, setSearchParams]);

  useEffect(() => {
    if (!name || !selectedVersion) {
      return;
    }
    loadDependencies(name, selectedVersion);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ecosystem, name, selectedVersion, pythonVersion, nodeVersion, cpu, os, target, features, defaultFeatures, includeOptional]);

  useEffect(() => {
    if (!name || !selectedVersion) {
      setVersionDetail(null);
      return undefined;
    }
    let cancelled = false;
    fetchVersion(ecosystem, name, selectedVersion)
      .then((payload) => {
        if (!cancelled) {
          setVersionDetail(payload);
        }
      })
      .catch(() => {
        // The sources block is auxiliary: the page stays usable without it.
        if (!cancelled) {
          setVersionDetail(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [ecosystem, name, selectedVersion]);

  async function loadPackage() {
    setLoading(true);
    setError("");
    try {
      const packagePayload = await fetchPackage(ecosystem, name);
      setPkg(packagePayload);

      const versionsPayload = await fetchVersions(ecosystem, name, { limit: 30 });
      const loadedVersions = versionsPayload.data || [];
      setVersions(loadedVersions);

      const vulnerabilitiesPayload = await fetchVulnerabilities(ecosystem, name);
      setVulns(vulnerabilitiesPayload.data || []);

      const versionExists = loadedVersions.some((row) => row.version === selectedVersion);
      const targetVersion = versionExists ? selectedVersion : (packagePayload.latest_version || loadedVersions[0]?.version || "");
      setSelectedVersion(targetVersion);
    } catch (err) {
      setError(err?.message || "Package request failed");
      setPkg(null);
      setVersions([]);
      setVulns([]);
      setDeps([]);
      setDepsUnknown(false);
      setSelectedVersion("");
    } finally {
      setLoading(false);
    }
  }

  async function loadDependencies(packageName, version) {
    setDepsLoading(true);
    setError("");
    try {
      const depPayload = await fetchDependencies(
        ecosystem,
        packageName,
        version,
        treeParams(
          ecosystem,
          { python: pythonVersion, node: nodeVersion, cpu, os: treeOs, target: treeTarget, features, defaultFeatures },
          includeOptional
        )
      );
      setDeps(depPayload.dependencies || []);
      setDepsUnknown(Boolean(depPayload.dependencies_unknown));
      setCollapsedNodes(new Set());
    } catch (err) {
      setError(err?.message || "Dependency tree request failed");
      setDeps([]);
      setDepsUnknown(false);
    } finally {
      setDepsLoading(false);
    }
  }

  function applyFeatures() {
    setFeatures(normalizeFeatures(featuresInput));
  }

  function refreshTree() {
    if (!selectedVersion) {
      return;
    }
    // A changed features field reloads the tree through the effect; an unchanged one is reloaded here.
    const typed = normalizeFeatures(featuresInput);
    if (typed !== features) {
      setFeatures(typed);
    } else {
      loadDependencies(name, selectedVersion);
    }
  }

  function toggleNodeCollapse(nodeId) {
    setCollapsedNodes((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  }

  function toggleTreeCollapse() {
    setCollapsedNodes((current) => {
      if (allNodeIds.length > 0 && allNodeIds.every((nodeId) => current.has(nodeId))) {
        return new Set();
      }
      return new Set(allNodeIds);
    });
  }

  return (
    <section>
      <h2>
        Package: {name}
        <span className="tag eco-badge">{ECOSYSTEM_LABELS[ecosystem] || ecosystem}</span>
      </h2>
      {error ? <p className="error">{error}</p> : null}
      {loading ? <p>Loading...</p> : null}

      {pkg ? (
        <>
          {deprecated ? (
            <div className="banner warn" role="alert">
              <b>Deprecated</b>
              {deprecated !== true ? `: ${deprecated}` : ""}
            </div>
          ) : null}
          {/* Go modules have no description, license or authors in the catalog: no empty blocks for them. */}
          {descriptionParagraphs.length || !isGo ? (
            <div className="package-description">
              {descriptionParagraphs.length ? (
                descriptionParagraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)
              ) : (
                <p>No description</p>
              )}
            </div>
          ) : null}
          <div className="meta-grid">
            {pkg.purl ? (
              <span>
                <b>purl</b>: <code>{pkg.purl}</code>
              </span>
            ) : null}
            <span>
              <b>Latest</b>: {latestVersion}
            </span>
            {!isGo || pkg.license_spdx || pkg.license_raw ? (
              <span>
                <b>License</b>: {pkg.license_spdx || pkg.license_raw || "unknown"}
              </span>
            ) : null}
            {!isGo || pkg.homepage_url ? (
              <span>
                <b>Homepage</b>: <UrlValue url={pkg.homepage_url} />
              </span>
            ) : null}
            {!isGo || pkg.repository_url ? (
              <span>
                <b>Repo</b>: <UrlValue url={pkg.repository_url} />
              </span>
            ) : null}
            {pkg.documentation_url ? (
              <span>
                <b>Documentation</b>: <UrlValue url={pkg.documentation_url} />
              </span>
            ) : null}
          </div>

          {pkg.sources?.length ? (
            <>
              <h3>Sources</h3>
              <ul className="result-list package-sources">
                {pkg.sources.map((source) => (
                  <li key={source.registry}>
                    <code>{source.registry}</code>
                    {source.primary ? <span className="tag">primary</span> : null}
                    {!source.enabled ? <span className="tag muted-tag">disabled</span> : null}
                    {source.missing_since ? (
                      <span className="tag warn">missing since {formatDateTime(source.missing_since)}</span>
                    ) : null}
                    {source.checked_at ? (
                      <span className="muted"> checked {formatDateTime(source.checked_at)}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}

          <h3>Versions</h3>
          <ul className="inline-list">
            {versions.slice(0, 10).map((v) => (
              <li key={v.version}>
                {v.version}
                <VersionStatus version={v} isGo={isGo} />
                {formatDate(v.release_date) ? <span className="muted version-date">{formatDate(v.release_date)}</span> : null}
              </li>
            ))}
          </ul>

          <h3>Dependency tree ({latestVersion || "n/a"})</h3>
          <div className="toolbar">
            <label>
              Version
              <select
                aria-label="Version"
                value={selectedVersion}
                onChange={(e) => setSelectedVersion(e.target.value)}
                disabled={loading || versions.length === 0}
              >
                {versions.map((v) => (
                  <option key={v.version} value={v.version}>
                    {v.version}
                  </option>
                ))}
              </select>
            </label>
            {isNpm ? (
              <>
                <label>
                  Node.js version
                  <select
                    aria-label="Node.js version"
                    value={nodeVersion}
                    onChange={(e) => setNodeVersion(e.target.value)}
                    disabled={loading || depsLoading}
                  >
                    {NPM_NODE_VERSIONS.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  OS
                  <select
                    aria-label="OS"
                    value={treeOs}
                    onChange={(e) => setOs(e.target.value)}
                    disabled={loading || depsLoading}
                  >
                    {NPM_OS_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  CPU
                  <select
                    aria-label="CPU"
                    value={cpu}
                    onChange={(e) => setCpu(e.target.value)}
                    disabled={loading || depsLoading}
                  >
                    {NPM_CPU_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            ) : isGo ? null : isCrates ? (
              <>
                <label>
                  Target
                  <select
                    aria-label="Target"
                    value={treeTarget}
                    onChange={(e) => setTarget(e.target.value)}
                    disabled={loading || depsLoading}
                  >
                    {CRATES_TARGETS.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Features
                  <input
                    aria-label="Features"
                    placeholder="derive, std"
                    value={featuresInput}
                    onChange={(e) => setFeaturesInput(e.target.value)}
                    onBlur={applyFeatures}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        applyFeatures();
                      }
                    }}
                  />
                </label>
                <label>
                  <input
                    type="checkbox"
                    aria-label="Default features"
                    checked={defaultFeatures}
                    onChange={(e) => setDefaultFeatures(e.target.checked)}
                  />
                  default features
                </label>
              </>
            ) : (
              <>
                <label>
                  Python version
                  <select
                    aria-label="Python version"
                    value={pythonVersion}
                    onChange={(e) => setPythonVersion(e.target.value)}
                    disabled={loading || depsLoading}
                  >
                    {PYTHON_VERSIONS.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  OS
                  <select
                    aria-label="OS"
                    value={os}
                    onChange={(e) => setOs(e.target.value)}
                    disabled={loading || depsLoading}
                  >
                    {OS_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            )}
            {isGo ? null : (
              <label>
                <input
                  type="checkbox"
                  checked={includeOptional}
                  onChange={(e) => setIncludeOptional(e.target.checked)}
                />
                include optional
              </label>
            )}
            <button type="button" onClick={toggleTreeCollapse} disabled={depsLoading || deps.length === 0}>
              {isTreeCollapsed ? "Expand tree" : "Collapse tree"}
            </button>
            <button
              type="button"
              onClick={refreshTree}
              disabled={loading || depsLoading || !selectedVersion}
            >
              Refresh tree
            </button>
          </div>
          {versionDetail && versionDetail.version === selectedVersion ? (
            <div className="version-detail">
              <b>Sources of {versionDetail.version}</b>:
              {versionDetail.sources?.length ? (
                <ul className="inline-list">
                  {versionDetail.sources.map((source) => (
                    <li key={source.registry}>
                      {source.registry}: {source.status}
                      {!source.enabled ? <span className="tag muted-tag">disabled</span> : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {versionDetail.dependencies_state === "unavailable" ? (
                <span className="tag warn">dependencies unknown</span>
              ) : null}
              {isGo && versionDetail.status === "yanked" ? (
                <p className="muted">
                  Retracted{versionDetail.status_reason ? `: ${versionDetail.status_reason}` : ""}
                </p>
              ) : null}
            </div>
          ) : null}
          {isCrates && versionDetail && versionDetail.version === selectedVersion ? (
            <FeaturesBlock version={versionDetail.version} features={versionDetail.features} />
          ) : null}
          {depsLoading ? <p>Loading dependency tree...</p> : null}
          {!depsLoading && depsUnknown ? (
            <p>Dependencies unknown: the source has no metadata for this version</p>
          ) : null}
          {!depsLoading && !depsUnknown && deps.length === 0 ? <p>No runtime dependencies for this environment.</p> : null}
          <DependencyTree
            nodes={deps}
            packageName={pkg.normalized_name}
            collapsedNodes={collapsedNodes}
            onToggleCollapse={toggleNodeCollapse}
          />

          <h3>Vulnerabilities</h3>
          {vulns.length === 0 ? <p>None</p> : null}
          <ul className="result-list">
            {vulns.map((vuln) => (
              <li key={vuln.source_id}>
                <b>{vuln.source_id}</b>
                {vuln.severity ? <span className="tag warn">{vuln.severity}</span> : null}
                <p>{vuln.summary}</p>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
