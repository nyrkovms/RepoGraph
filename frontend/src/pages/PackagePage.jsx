import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { fetchArtifacts, fetchDependencies, fetchPackage, fetchVersion, fetchVersions, fetchVulnerabilities } from "../lib/api";
import { getPackageDescriptionParagraphs } from "../lib/packageDescription";
import { OS_OPTIONS, PYTHON_VERSIONS } from "../lib/runtimeEnv";
import { DependencyTree } from "./package/DependencyTree";
import { FeaturesBlock } from "./package/FeaturesBlock";
import { RelocationLink, UrlValue } from "./package/Links";
import {
  CRATES_TARGETS,
  DEFAULTS,
  ECOSYSTEM_LABELS,
  MAVEN_SCOPES,
  NPM_CPU_OPTIONS,
  NPM_NODE_VERSIONS,
  NPM_OS_OPTIONS,
  NUGET_FRAMEWORKS,
  RPM_ARCHES,
  VERSIONS_SHOWN,
  archesOf,
  normalizeFeatures,
  normalizeScopes,
  pickOption,
  treeParams,
} from "./package/packageEnv";
import {
  collectNodeIds,
  deprecationMessage,
  emptyVulnerabilitiesText,
  formatDate,
  formatDateTime,
  initialVersion,
  packagingOf,
} from "./package/packageHelpers";
import { VersionStatus } from "./package/VersionStatus";


export default function PackagePage() {
  // `/package/:ecosystem/*` carries native coordinates (`@angular/core`); `/package/:name` is the old PyPI route.
  const params = useParams();
  const ecosystem = params.ecosystem || "pypi";
  const name = params.ecosystem ? params["*"] : params.name;
  const isNpm = ecosystem === "npm";
  const isCrates = ecosystem === "crates";
  const isGo = ecosystem === "go";
  const isNuget = ecosystem === "nuget";
  const isMaven = ecosystem === "maven";
  const isRpm = ecosystem === "rpm";
  const [searchParams, setSearchParams] = useSearchParams();

  const [pkg, setPkg] = useState(null);
  const [versions, setVersions] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState(() => initialVersion(searchParams.get("version")));
  const [pythonVersion, setPythonVersion] = useState(() => searchParams.get("python") || DEFAULTS.pypi.python);
  const [nodeVersion, setNodeVersion] = useState(() => searchParams.get("node") || DEFAULTS.npm.node);
  const [cpu, setCpu] = useState(() => searchParams.get("cpu") || DEFAULTS.npm.cpu);
  const [os, setOs] = useState(() => searchParams.get("os") || DEFAULTS.pypi.os);
  const [target, setTarget] = useState(() => searchParams.get("target") || DEFAULTS.crates.target);
  const [framework, setFramework] = useState(() => searchParams.get("target_framework") || DEFAULTS.nuget.framework);
  const [scopes, setScopes] = useState(() => normalizeScopes(searchParams.get("scopes")));
  const [arch, setArch] = useState(() => pickOption(searchParams.get("arch"), RPM_ARCHES, DEFAULTS.rpm.arch));
  // RPM: the arches of each version, from its artifacts, by `<name>/<version>`.
  const [archesByVersion, setArchesByVersion] = useState({});
  // The features field is applied (and the tree reloaded) on Enter or blur, not on every keystroke.
  const [features, setFeatures] = useState(() => normalizeFeatures(searchParams.get("features")));
  const [featuresInput, setFeaturesInput] = useState(() => normalizeFeatures(searchParams.get("features")));
  const [defaultFeatures, setDefaultFeatures] = useState(() => searchParams.get("default_features") !== "0");
  const [vulns, setVulns] = useState([]);
  // Whether any source feeds advisories for this ecosystem: an empty list means nothing without it (#68).
  const [vulnCoverage, setVulnCoverage] = useState("not_collected");
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
  const treeFramework = pickOption(framework, NUGET_FRAMEWORKS, DEFAULTS.nuget.framework);
  const selectedScopes = scopes.split(",");
  const latestVersion = useMemo(() => selectedVersion || pkg?.latest_version || versions?.[0]?.version, [selectedVersion, pkg, versions]);
  // `deprecation.message` of the package (Go: the `// Deprecated:` paragraph of go.mod; NuGet: the message or the reasons), `true` when it has no text.
  const deprecated = useMemo(() => {
    if (!pkg?.deprecation) {
      return "";
    }
    return deprecationMessage(pkg.deprecation) || true;
  }, [pkg?.deprecation]);
  const relocatedTo = pkg?.deprecation?.alternative || "";
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
    setSelectedVersion(initialVersion(searchParams.get("version")));
    setPythonVersion(searchParams.get("python") || DEFAULTS.pypi.python);
    setNodeVersion(searchParams.get("node") || DEFAULTS.npm.node);
    setCpu(searchParams.get("cpu") || DEFAULTS.npm.cpu);
    setOs(searchParams.get("os") || DEFAULTS.pypi.os);
    setTarget(searchParams.get("target") || DEFAULTS.crates.target);
    setFramework(searchParams.get("target_framework") || DEFAULTS.nuget.framework);
    setScopes(normalizeScopes(searchParams.get("scopes")));
    setArch(pickOption(searchParams.get("arch"), RPM_ARCHES, DEFAULTS.rpm.arch));
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
    } else if (isNuget) {
      if (framework && framework !== DEFAULTS.nuget.framework) {
        next.set("target_framework", framework);
      }
    } else if (isMaven) {
      if (scopes !== DEFAULTS.maven.scopes) {
        next.set("scopes", scopes);
      }
    } else if (isRpm) {
      if (arch !== DEFAULTS.rpm.arch) {
        next.set("arch", arch);
      }
    } else if (!isGo && pythonVersion && pythonVersion !== DEFAULTS.pypi.python) {
      next.set("python", pythonVersion);
    }
    if (!isCrates && !isGo && !isNuget && !isMaven && !isRpm && os && os !== DEFAULTS.pypi.os) {
      next.set("os", os);
    }
    if (includeOptional && !isGo && !isNuget && !isMaven) {
      next.set("include_optional", "1");
    }
    setSearchParams(next, { replace: true });
  }, [isNpm, isCrates, isGo, isNuget, isMaven, isRpm, selectedVersion, pythonVersion, nodeVersion, cpu, os, target, framework, scopes, arch, features, defaultFeatures, includeOptional, setSearchParams]);

  useEffect(() => {
    if (!name || !selectedVersion) {
      return;
    }
    loadDependencies(name, selectedVersion);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ecosystem, name, selectedVersion, pythonVersion, nodeVersion, cpu, os, target, framework, scopes, arch, features, defaultFeatures, includeOptional]);

  // RPM: the listed versions and the selected one are asked for their files, whose arches the page shows. A failed
  // request leaves that version without arches; the page does not depend on them.
  useEffect(() => {
    if (!isRpm || !name) {
      return undefined;
    }
    const wanted = new Set(versions.slice(0, VERSIONS_SHOWN).map((row) => row.version));
    if (selectedVersion) {
      wanted.add(selectedVersion);
    }
    wanted.forEach((version) => {
      const key = `${name}/${version}`;
      if (archesByVersion[key]) {
        return;
      }
      // Keyed by package, so an answer that arrives after the page moved on is still the right one for its key.
      fetchArtifacts(ecosystem, name, version)
        .then((payload) => setArchesByVersion((current) => ({ ...current, [key]: archesOf(payload?.data) })))
        .catch(() => {});
    });
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRpm, ecosystem, name, versions, selectedVersion]);

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
      setVulnCoverage(vulnerabilitiesPayload.coverage || "not_collected");

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
          { python: pythonVersion, node: nodeVersion, cpu, os: treeOs, target: treeTarget, framework: treeFramework, scopes, arch, features, defaultFeatures },
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

  // At least one scope stays selected: the API refuses an empty list.
  function toggleScope(scope, checked) {
    const next = new Set(selectedScopes);
    if (checked) {
      next.add(scope);
    } else {
      next.delete(scope);
    }
    if (next.size > 0) {
      setScopes(MAVEN_SCOPES.filter((item) => next.has(item)).join(","));
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
        Package: {(isNuget || isMaven || isRpm) && pkg?.name ? pkg.name : name}
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
              {relocatedTo ? (
                <>
                  {" "}
                  (moved to <RelocationLink alternative={relocatedTo} isMaven={isMaven} />)
                </>
              ) : null}
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
            {isMaven ? (
              <>
                <span>
                  <b>groupId</b>: <code>{pkg.namespace}</code>
                </span>
                <span>
                  <b>artifactId</b>: <code>{pkg.normalized_name}</code>
                </span>
              </>
            ) : null}
            {isRpm ? (
              <>
                <span>
                  <b>Distro</b>: <code>{pkg.namespace}</code>
                </span>
                <span>
                  <b>Name</b>: <code>{pkg.normalized_name}</code>
                </span>
              </>
            ) : null}
            <span>
              <b>Latest</b>: {latestVersion}
            </span>
            {!isGo || pkg.license_spdx || pkg.license_raw ? (
              <span>
                <b>License</b>: {pkg.license_spdx || pkg.license_raw || "Not published by the source"}
              </span>
            ) : null}
            {pkg.homepage_url ? (
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
            {versions.slice(0, VERSIONS_SHOWN).map((v) => (
              <li key={v.version}>
                {v.version}
                <VersionStatus version={v} isGo={isGo} />
                {isMaven && packagingOf(v) ? <span className="tag cond">{packagingOf(v)}</span> : null}
                {isRpm
                  ? (archesByVersion[`${name}/${v.version}`] || []).map((item) => (
                      <span key={item} className="tag cond">
                        {item}
                      </span>
                    ))
                  : null}
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
            ) : isGo ? null : isRpm ? (
              <label>
                Arch
                <select aria-label="Arch" value={arch} onChange={(e) => setArch(e.target.value)} disabled={loading || depsLoading}>
                  {RPM_ARCHES.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
            ) : isMaven ? (
              <div className="scope-select" role="group" aria-label="Scopes">
                <b>Scopes</b>
                {MAVEN_SCOPES.map((scope) => (
                  <label key={scope}>
                    <input
                      type="checkbox"
                      checked={selectedScopes.includes(scope)}
                      onChange={(e) => toggleScope(scope, e.target.checked)}
                      disabled={loading || depsLoading}
                    />
                    {scope}
                  </label>
                ))}
              </div>
            ) : isNuget ? (
              <label>
                Target framework
                <select
                  aria-label="Target framework"
                  value={treeFramework}
                  onChange={(e) => setFramework(e.target.value)}
                  disabled={loading || depsLoading}
                >
                  {NUGET_FRAMEWORKS.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
            ) : isCrates ? (
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
            {isGo || isNuget || isMaven ? null : (
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
              {isRpm && archesByVersion[`${name}/${versionDetail.version}`]?.length ? (
                <p className="muted">Arches: {archesByVersion[`${name}/${versionDetail.version}`].join(", ")}</p>
              ) : null}
              {isRpm && versionDetail.status === "superseded" ? (
                <p className="muted">Superseded: a newer build of the same arch is in the source, this one is still in the catalog.</p>
              ) : null}
              {isMaven && packagingOf(versionDetail) ? (
                <p className="muted">
                  Packaging: <code>{packagingOf(versionDetail)}</code>
                </p>
              ) : null}
              {isNuget && versionDetail.status === "unlisted" ? (
                <p className="muted">Unlisted: the author hid this version from search, it can still be restored.</p>
              ) : null}
              {versionDetail.deprecation ? (
                <p className="muted">
                  Deprecated
                  {deprecationMessage(versionDetail.deprecation) ? `: ${deprecationMessage(versionDetail.deprecation)}` : ""}
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
            ecosystem={ecosystem}
            arch={arch}
            collapsedNodes={collapsedNodes}
            onToggleCollapse={toggleNodeCollapse}
          />

          <h3>Vulnerabilities</h3>
          {vulns.length === 0 ? <p>{emptyVulnerabilitiesText(vulnCoverage, selectedVersion === pkg.latest_version)}</p> : null}
          {vulns.length > 0 && selectedVersion !== pkg.latest_version ? (
            <p className="muted">Listed for the package, not for this version</p>
          ) : null}
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
