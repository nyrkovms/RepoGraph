import { Link } from "react-router-dom";

import { DEFAULTS } from "./packageEnv";
import { collectDependencyConditions } from "./packageHelpers";

// The package that provides an RPM capability (`resolved_name`, found by rpm-worker), linked to its page at the
// version the tree resolved.
function ProviderLink({ node, arch }) {
  const query = new URLSearchParams();
  if (node.resolved_version) {
    query.set("version", node.resolved_version);
  }
  if (arch && arch !== DEFAULTS.rpm.arch) {
    query.set("arch", arch);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return (
    <span className="muted dep-provider">
      provided by <Link to={`/package/rpm/${node.resolved_name}${suffix}`}>{node.resolved_name}</Link>
    </span>
  );
}

function DependencyTreeNode({ node, nodeId, packageName, ecosystem, arch, collapsedNodes, onToggleCollapse }) {
  const conditions = collectDependencyConditions(node, ecosystem);
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
          {ecosystem === "rpm" && node.resolved_name ? <ProviderLink node={node} arch={arch} /> : null}
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
          ecosystem={ecosystem}
          arch={arch}
          parentId={nodeId}
          collapsedNodes={collapsedNodes}
          onToggleCollapse={onToggleCollapse}
        />
      ) : null}
    </li>
  );
}

export function DependencyTree({ nodes, packageName, ecosystem, arch, parentId = "root", collapsedNodes, onToggleCollapse }) {
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
          ecosystem={ecosystem}
          arch={arch}
          collapsedNodes={collapsedNodes}
          onToggleCollapse={onToggleCollapse}
        />
      ))}
    </ul>
  );
}
