// The dependency tree response as a table: one row per distinct package and resolved version.

// The API stops expanding a tree at this depth (FULL_TREE_MAX_DEPTH); the page does not ask for less.
export const TREE_DEPTH_LIMIT = 200;

function add(list, value) {
  if (value && !list.includes(value)) {
    list.push(value);
  }
}

// The catalog name a resolved node leads to: `resolved_name` when the node is not the package itself (an RPM
// capability, a spelling the catalog normalises), else its own name. Null when no version was resolved.
function catalogName(node) {
  return node.resolved_version ? node.resolved_name || node.name : null;
}

// Rows in the order of the tree walk (a node before its children). A package reached both directly and indirectly is
// Direct; an indirect row keeps the path (names between the root and the package) of its first occurrence. A cycle
// node is a repeat of a package listed elsewhere: it adds no row and has no children to walk. A node without a
// resolved version is one row per name, with `resolved` null.
export function flattenTree(nodes) {
  const rows = new Map();

  function walk(list, path) {
    for (const node of list || []) {
      if (node.cycle) {
        continue;
      }
      const ref = catalogName(node);
      const key = `${ref || node.name}@${node.resolved_version || ""}`;
      const direct = path.length === 0;
      let row = rows.get(key);
      if (!row) {
        row = {
          key,
          name: ref || node.name,
          ref,
          capability: ref && node.name !== ref ? node.name : "",
          requires: [],
          resolved: node.resolved_version || null,
          types: [],
          relation: direct ? "direct" : "indirect",
          license: node.license || "",
          path,
          depth: path.length + 1,
          dependenciesUnknown: Boolean(node.dependencies_unknown),
        };
        rows.set(key, row);
        add(row.requires, node.constraint !== "*" ? node.constraint : "");
        add(row.types, node.dependency_type);
      } else if (direct) {
        if (row.relation !== "direct") {
          // First seen below another package, now directly: the row describes the direct edges from here on.
          Object.assign(row, { relation: "direct", path: [], depth: 1, requires: [], types: [], capability: ref && node.name !== ref ? node.name : "" });
        }
        add(row.requires, node.constraint !== "*" ? node.constraint : "");
        add(row.types, node.dependency_type);
      }
      walk(node.children, [...path, node.name]);
    }
  }

  walk(nodes, []);
  return [...rows.values()].sort(
    (a, b) => Number(b.relation === "direct") - Number(a.relation === "direct") || a.name.localeCompare(b.name) || (a.resolved || "").localeCompare(b.resolved || "")
  );
}

export function summarize(rows) {
  const licenses = new Map();
  rows.forEach((row) => {
    if (row.resolved) {
      const name = row.license || "unknown";
      licenses.set(name, (licenses.get(name) || 0) + 1);
    }
  });
  return {
    direct: rows.filter((row) => row.relation === "direct").length,
    indirect: rows.filter((row) => row.relation === "indirect").length,
    notInCatalog: rows.filter((row) => !row.resolved).length,
    licenses: [...licenses.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
  };
}
