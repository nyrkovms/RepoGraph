import { OS_OPTIONS, PYTHON_VERSIONS } from "../../lib/runtimeEnv";
import { DependencyTree } from "./DependencyTree";
import {
  CRATES_TARGETS,
  MAVEN_SCOPES,
  NPM_CPU_OPTIONS,
  NPM_NODE_VERSIONS,
  NPM_OS_OPTIONS,
  NUGET_FRAMEWORKS,
  RPM_ARCHES,
} from "./packageEnv";

// The Dependencies tab: the environment controls and the dependency tree.
export function DependenciesTab({
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
      <h3>Dependency tree ({latestVersion || "n/a"})</h3>
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

    </>
  );
}
