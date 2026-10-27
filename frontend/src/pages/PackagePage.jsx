import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { fetchDependencies, fetchPackage, fetchVersions, fetchVulnerabilities } from "../lib/api";
import { getPackageDescriptionParagraphs } from "../lib/packageDescription";
import { OS_OPTIONS, PYTHON_VERSIONS } from "../lib/runtimeEnv";

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

function collectDependencyConditions(node) {
  const labels = [];
  const extraValues = new Set();
  const conditions = Array.isArray(node.environment_markers?.conditions) ? node.environment_markers.conditions : [];

  if (node.dependency_type === "optional") {
    labels.push("optional dependency");
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
  const { name } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();

  const [pkg, setPkg] = useState(null);
  const [versions, setVersions] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState(() => searchParams.get("version") || "");
  const [pythonVersion, setPythonVersion] = useState(() => searchParams.get("python") || "3.12");
  const [os, setOs] = useState(() => searchParams.get("os") || "linux");
  const [vulns, setVulns] = useState([]);
  const [deps, setDeps] = useState([]);
  const [includeOptional, setIncludeOptional] = useState(() => searchParams.get("include_optional") === "1");
  const [loading, setLoading] = useState(false);
  const [depsLoading, setDepsLoading] = useState(false);
  const [error, setError] = useState("");
  const [collapsedNodes, setCollapsedNodes] = useState(() => new Set());

  const latestVersion = useMemo(() => selectedVersion || pkg?.latest_version || versions?.[0]?.version, [selectedVersion, pkg, versions]);
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
    setPythonVersion(searchParams.get("python") || "3.12");
    setOs(searchParams.get("os") || "linux");
    setIncludeOptional(searchParams.get("include_optional") === "1");
    setDeps([]);
    setCollapsedNodes(new Set());
    loadPackage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  useEffect(() => {
    const next = new URLSearchParams();
    if (selectedVersion) {
      next.set("version", selectedVersion);
    }
    if (pythonVersion && pythonVersion !== "3.12") {
      next.set("python", pythonVersion);
    }
    if (os && os !== "linux") {
      next.set("os", os);
    }
    if (includeOptional) {
      next.set("include_optional", "1");
    }
    setSearchParams(next, { replace: true });
  }, [selectedVersion, pythonVersion, os, includeOptional, setSearchParams]);

  useEffect(() => {
    if (!name || !selectedVersion) {
      return;
    }
    loadDependencies(name, selectedVersion);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, selectedVersion, pythonVersion, os, includeOptional]);

  async function loadPackage() {
    setLoading(true);
    setError("");
    try {
      const packagePayload = await fetchPackage(name);
      setPkg(packagePayload);

      const versionsPayload = await fetchVersions(name, { limit: 30 });
      const loadedVersions = versionsPayload.data || [];
      setVersions(loadedVersions);

      const vulnerabilitiesPayload = await fetchVulnerabilities(name);
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
      setSelectedVersion("");
    } finally {
      setLoading(false);
    }
  }

  async function loadDependencies(packageName, version) {
    setDepsLoading(true);
    setError("");
    try {
      const depPayload = await fetchDependencies(packageName, version, {
        include_optional: includeOptional,
        python_version: pythonVersion,
        os,
      });
      setDeps(depPayload.dependencies || []);
      setCollapsedNodes(new Set());
    } catch (err) {
      setError(err?.message || "Dependency tree request failed");
      setDeps([]);
    } finally {
      setDepsLoading(false);
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
      <h2>Package: {name}</h2>
      {error ? <p className="error">{error}</p> : null}
      {loading ? <p>Loading...</p> : null}

      {pkg ? (
        <>
          <div className="package-description">
            {descriptionParagraphs.length ? (
              descriptionParagraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)
            ) : (
              <p>No description</p>
            )}
          </div>
          <div className="meta-grid">
            <span>
              <b>Latest</b>: {latestVersion}
            </span>
            <span>
              <b>License</b>: {pkg.license_spdx || pkg.license_raw || "unknown"}
            </span>
            <span>
              <b>Homepage</b>: {pkg.homepage_url || "n/a"}
            </span>
            <span>
              <b>Repo</b>: {pkg.repository_url || "n/a"}
            </span>
          </div>

          <h3>Versions</h3>
          <ul className="inline-list">
            {versions.slice(0, 10).map((v) => (
              <li key={v.version}>
                {v.version}
                <span className="tag">{v.status}</span>
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
            <label>
              <input
                type="checkbox"
                checked={includeOptional}
                onChange={(e) => setIncludeOptional(e.target.checked)}
              />
              include optional
            </label>
            <button type="button" onClick={toggleTreeCollapse} disabled={depsLoading || deps.length === 0}>
              {isTreeCollapsed ? "Expand tree" : "Collapse tree"}
            </button>
            <button
              type="button"
              onClick={() => {
                if (selectedVersion) {
                  loadDependencies(name, selectedVersion);
                }
              }}
              disabled={loading || depsLoading || !selectedVersion}
            >
              Refresh tree
            </button>
          </div>
          {depsLoading ? <p>Loading dependency tree...</p> : null}
          {!depsLoading && deps.length === 0 ? <p>No runtime dependencies for this environment.</p> : null}
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
