import { useEffect, useMemo, useState } from "react";

import { resolveDependencies } from "../lib/api";
import { buildRuntimeEnvironment, OS_OPTIONS, PYTHON_VERSIONS } from "../lib/runtimeEnv";

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
    ids.push(...collectNodeIds(node.dependencies || [], nodeId));
  });
  return ids;
}

function ResolveTreeNode({ node, nodeId, ownerPackageName, collapsedNodes, onToggleCollapse }) {
  const hasChildren = Boolean(node.dependencies?.length);
  const isCollapsed = collapsedNodes.has(nodeId);
  const conditions = collectDependencyConditions(node);
  const installHints = ownerPackageName ? conditions.extras.map((extra) => `${ownerPackageName}[${extra}]`) : [];

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
          <span className="tag dep-version-tag">{node.version}</span>
          {node.hasNewerCompatibleVersion && node.latestCompatibleVersion ? (
            <span className="tag">newer {node.latestCompatibleVersion}</span>
          ) : null}
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
        <ResolveTree
          nodes={node.dependencies}
          parentId={nodeId}
          ownerPackageName={node.name}
          collapsedNodes={collapsedNodes}
          onToggleCollapse={onToggleCollapse}
        />
      ) : null}
    </li>
  );
}

function ResolveTree({ nodes, parentId = "root", ownerPackageName = null, collapsedNodes, onToggleCollapse }) {
  if (!nodes?.length) {
    return null;
  }

  return (
    <ul className="dep-tree">
      {nodes.map((node, index) => (
        <ResolveTreeNode
          key={`${node.name}-${node.version}-${index}`}
          node={node}
          nodeId={`${parentId}/${node.name}-${index}`}
          ownerPackageName={ownerPackageName}
          collapsedNodes={collapsedNodes}
          onToggleCollapse={onToggleCollapse}
        />
      ))}
    </ul>
  );
}

function parseInput(raw) {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^([A-Za-z0-9._-]+)(?:\[([A-Za-z0-9._,\s-]+)\])?\s*(.*)$/);
      if (!match) {
        return {
          name: line,
          version_spec: "*",
          extras: [],
        };
      }

      const [, name, extrasRaw = "", versionSpecRaw = ""] = match;
      return {
        name,
        version_spec: versionSpecRaw.trim() || "*",
        extras: extrasRaw
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      };
    });
}

export default function ResolvePage() {
  const [depsText, setDepsText] = useState("flask >=3.0\nrequests >=2.31");
  const [inputMode, setInputMode] = useState("requirements");
  const [pythonVersion, setPythonVersion] = useState("3.12");
  const [os, setOs] = useState("linux");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [collapsedNodes, setCollapsedNodes] = useState(() => new Set());

  const parsedInput = useMemo(() => parseInput(depsText), [depsText]);
  const rootEntries = useMemo(() => Object.entries(result?.resolved || {}), [result]);
  const rootNodeList = useMemo(
    () =>
      rootEntries.map(([name, pkg]) => ({
        name,
        version: pkg.version,
        constraint: parsedInput.find((item) => item.name === name)?.version_spec || "*",
        latestCompatibleVersion: inputMode === "freeze" ? pkg.latest_compatible_version : null,
        hasNewerCompatibleVersion: inputMode === "freeze" ? Boolean(pkg.has_newer_compatible_version) : false,
        dependencies: pkg.dependencies || [],
      })),
    [inputMode, parsedInput, rootEntries]
  );
  const allNodeIds = useMemo(() => collectNodeIds(rootNodeList), [rootNodeList]);
  const isTreeCollapsed = useMemo(
    () => allNodeIds.length > 0 && allNodeIds.every((nodeId) => collapsedNodes.has(nodeId)),
    [allNodeIds, collapsedNodes]
  );

  useEffect(() => {
    const handler = () => {
      setResult(null);
      setError("");
      setCollapsedNodes(new Set());
    };
    window.addEventListener("phub-token-updated", handler);
    return () => window.removeEventListener("phub-token-updated", handler);
  }, []);

  async function onResolve() {
    setLoading(true);
    setError("");
    setResult(null);
    setCollapsedNodes(new Set());

    try {
      const dependencies = parseInput(depsText);
      const payload = {
        ecosystem: "pypi",
        input_mode: inputMode,
        dependencies,
        environment: buildRuntimeEnvironment(pythonVersion, os),
        options: {
          include_dev: false,
          max_depth: 30,
          max_rounds: 200,
        },
      };
      const response = await resolveDependencies(payload);
      setResult(response);
    } catch (err) {
      setError(err?.message || "Resolve failed");
    } finally {
      setLoading(false);
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
      <h2>Resolve dependencies</h2>
      <div className="toolbar">
        <label>
          Input mode
          <select value={inputMode} onChange={(e) => setInputMode(e.target.value)}>
            <option value="requirements">Requirements</option>
            <option value="freeze">pip freeze</option>
          </select>
        </label>
        <label>
          Python version
          <select value={pythonVersion} onChange={(e) => setPythonVersion(e.target.value)}>
            {PYTHON_VERSIONS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label>
          OS
          <select value={os} onChange={(e) => setOs(e.target.value)}>
            {OS_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label>
        Dependencies (one per line: <code>name spec</code>)
        <textarea value={depsText} onChange={(e) => setDepsText(e.target.value)} rows={8} />
      </label>

      <button type="button" onClick={onResolve} disabled={loading}>
        {loading ? "Resolving..." : "Resolve"}
      </button>

      {error ? <p className="error">{error}</p> : null}

      {result ? (
        <section>
          <h3>Result</h3>
          <div className="toolbar">
            <p>
              mode: <code>{result.input_mode}</code>, strategy: <code>{result.resolve_strategy}</code>, packages: {result.total_packages}, time: {result.resolve_time_ms}ms
            </p>
            <button type="button" onClick={toggleTreeCollapse} disabled={rootNodeList.length === 0}>
              {isTreeCollapsed ? "Expand tree" : "Collapse tree"}
            </button>
          </div>
          <ResolveTree nodes={rootNodeList} collapsedNodes={collapsedNodes} onToggleCollapse={toggleNodeCollapse} />
        </section>
      ) : null}
    </section>
  );
}
