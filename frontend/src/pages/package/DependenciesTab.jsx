import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { OS_OPTIONS, PYTHON_VERSIONS } from "../../lib/runtimeEnv";
import { DependencyTree } from "./DependencyTree";
import { ActionLink } from "./Links";
import { TREE_DEPTH_LIMIT, flattenTree, summarize } from "./dependencyRows";
import {
  CRATES_TARGETS,
  MAVEN_SCOPES,
  NPM_CPU_OPTIONS,
  NPM_NODE_VERSIONS,
  NPM_OS_OPTIONS,
  NUGET_FRAMEWORKS,
  RPM_ARCHES,
  DEFAULTS,
} from "./packageEnv";

const dash = (items) => (items.length ? items.join(", ") : "—");

// The environment controls apply to both views; the tree's own buttons sit with them in the tree view only.
const VIEWS = [
  { value: "", label: "Table" },
  { value: "tree", label: "Tree" },
];

// The catalog page of a resolved row, at the version the tree resolved (an RPM provider also at the tree's arch).
function rowHref(row, ecosystem, arch) {
  const query = new URLSearchParams({ version: row.resolved });
  if (ecosystem === "rpm" && arch && arch !== DEFAULTS.rpm.arch) {
    query.set("arch", arch);
  }
  return `/package/${ecosystem}/${row.ref}?${query.toString()}`;
}

function Summary({ summary }) {
  return (
    <>
      <p className="dep-summary">{`${summary.direct} direct · ${summary.indirect} indirect · ${summary.notInCatalog} not resolved`}</p>
      {summary.licenses.length ? (
        <p className="dep-summary">{`Licenses: ${summary.licenses.map(([name, count]) => `${name} ${count}`).join(" · ")}`}</p>
      ) : null}
      <p className="muted dep-summary-note">For this environment. Overview counts latest versions across the catalog.</p>
    </>
  );
}

function RelationChips({ relation, summary, tabHref, onSelectRelation }) {
  const chips = [
    { value: "", label: "All", count: summary.direct + summary.indirect },
    { value: "direct", label: "Direct", count: summary.direct },
    { value: "indirect", label: "Indirect", count: summary.indirect },
  ];
  return (
    <nav className="chips" aria-label="Relation">
      {chips.map((chip) => (
        <ActionLink
          key={chip.value}
          href={tabHref("dependencies", { dep_relation: chip.value || null })}
          onSelect={() => onSelectRelation(chip.value)}
          className={`chip${chip.value === relation ? " active" : ""}`}
          aria-current={chip.value === relation ? "true" : undefined}
        >
          {`${chip.label} ${chip.count}`}
        </ActionLink>
      ))}
    </nav>
  );
}

function DependencyTable({ deps, ecosystem, arch, relation, tabHref, onSelectRelation }) {
  const [filter, setFilter] = useState("");
  const rows = useMemo(() => flattenTree(deps), [deps]);
  const summary = useMemo(() => summarize(rows), [rows]);
  const needle = filter.trim().toLowerCase();
  const shown = rows.filter(
    (row) => (!relation || row.relation === relation) && (!needle || row.name.toLowerCase().includes(needle) || row.capability.toLowerCase().includes(needle))
  );
  return (
    <>
      <Summary summary={summary} />
      <RelationChips relation={relation} summary={summary} tabHref={tabHref} onSelectRelation={onSelectRelation} />
      <p>
        <input type="search" aria-label="Filter by name" placeholder="Filter by name" value={filter} onChange={(e) => setFilter(e.target.value)} />
      </p>
      {shown.length === 0 ? <p className="muted">No dependencies match</p> : null}
      {shown.length > 0 ? (
        <div className="table-wrap">
          <table className="used-by-table dep-table">
            <thead>
              <tr>
                <th>Package</th>
                <th>Requires</th>
                <th>Resolved</th>
                <th>Type</th>
                <th>Relation</th>
                <th>License</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr key={row.key}>
                  <td>
                    {row.ref ? <Link to={rowHref(row, ecosystem, arch)}>{row.name}</Link> : <code>{row.name}</code>}
                    {row.capability ? <div className="muted dep-via">{`provides ${row.capability}`}</div> : null}
                    {row.path.length ? <div className="muted dep-via">{`via ${row.path.join(" → ")}`}</div> : null}
                  </td>
                  <td>{dash(row.requires)}</td>
                  <td>{row.resolved || <span className="tag muted-tag">not resolved</span>}</td>
                  <td>{dash(row.types)}</td>
                  <td>{row.relation === "direct" ? "Direct" : "Indirect"}</td>
                  <td>{row.license || "unknown"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {rows.some((row) => row.depth >= TREE_DEPTH_LIMIT) ? (
        <p className="muted">{`Dependencies deeper than ${TREE_DEPTH_LIMIT} levels are not listed.`}</p>
      ) : null}
    </>
  );
}

// The Dependencies tab: the environment controls, then the table (default) or the tree of the same response.
export function DependenciesTab({
  view,
  relation,
  tabHref,
  onSelectView,
  onSelectRelation,
  isNpm,
  isGo,
  isRpm,
  isMaven,
  isNuget,
  isCrates,
  nodeVersion,
  setNodeVersion,
  treeOs,
  setOs,
  os,
  cpu,
  setCpu,
  arch,
  setArch,
  selectedScopes,
  toggleScope,
  treeFramework,
  setFramework,
  treeTarget,
  setTarget,
  featuresInput,
  setFeaturesInput,
  applyFeatures,
  defaultFeatures,
  setDefaultFeatures,
  pythonVersion,
  setPythonVersion,
  includeOptional,
  setIncludeOptional,
  loading,
  depsLoading,
  depsUnknown,
  isTreeCollapsed,
  toggleTreeCollapse,
  refreshTree,
  selectedVersion,
  deps,
  latestVersion,
  pkg,
  ecosystem,
  collapsedNodes,
  toggleNodeCollapse,
}) {
  return (
    <>
      <h3>{view === "tree" ? "Dependency tree" : "Dependencies"} ({latestVersion || "n/a"})</h3>
      <div className="toolbar">
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
        {view === "tree" ? (
          <>
            <button type="button" onClick={toggleTreeCollapse} disabled={depsLoading || deps.length === 0}>
              {isTreeCollapsed ? "Expand tree" : "Collapse tree"}
            </button>
            <button type="button" onClick={refreshTree} disabled={loading || depsLoading || !selectedVersion}>
              Refresh tree
            </button>
          </>
        ) : null}
      </div>
      <nav className="chips" aria-label="Dependencies view">
        {VIEWS.map((item) => (
          <ActionLink
            key={item.value}
            href={tabHref("dependencies", { deps: item.value || null })}
            onSelect={() => onSelectView(item.value)}
            className={`chip${item.value === view ? " active" : ""}`}
            aria-current={item.value === view ? "true" : undefined}
          >
            {item.label}
          </ActionLink>
        ))}
      </nav>
      {depsLoading ? <p>{view === "tree" ? "Loading dependency tree..." : "Loading dependencies..."}</p> : null}
      {view === "tree" ? (
        <>
          {!depsLoading && depsUnknown ? <p>Dependencies unknown: the source has no metadata for this version</p> : null}
          {!depsLoading && !depsUnknown && deps.length === 0 ? <p>No runtime dependencies for this environment.</p> : null}
          <DependencyTree
            nodes={deps}
            packageName={pkg.normalized_name}
            ecosystem={ecosystem}
            arch={arch}
            collapsedNodes={collapsedNodes}
            onToggleCollapse={toggleNodeCollapse}
          />
        </>
      ) : null}
      {view !== "tree" && !depsLoading ? (
        depsUnknown ? (
          <p>The source has no dependency data for this version</p>
        ) : deps.length === 0 ? (
          <p>No dependencies for this environment.</p>
        ) : (
          <DependencyTable
            deps={deps}
            ecosystem={ecosystem}
            arch={arch}
            relation={relation}
            tabHref={tabHref}
            onSelectRelation={onSelectRelation}
          />
        )
      ) : null}
    </>
  );
}
