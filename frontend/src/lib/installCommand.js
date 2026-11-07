// The one command (or, for Maven, the coordinate) that installs a version, from the fields the package API returns:
// `name` as the registry spells it, `namespace` (npm scope without `@`, Maven groupId, RPM distro) and
// `normalized_name` (npm and Maven local name, the whole module path of Go, the RPM name without its distro).
export function installCommand(ecosystem, pkg, version) {
  const name = pkg?.name || pkg?.normalized_name || "";
  const local = pkg?.normalized_name || name;
  const namespace = String(pkg?.namespace || "");
  switch (ecosystem) {
    case "pypi":
      return `pip install ${name}==${version}`;
    case "npm": {
      const scope = namespace.replace(/^@/, "");
      const full = name.startsWith("@") ? name : scope ? `@${scope}/${local}` : local;
      return `npm i ${full}@${version}`;
    }
    case "crates":
      return `cargo add ${name}@${version}`;
    case "go": {
      // The catalog stores a module by its whole path; a namespace is prefixed only when the name lacks it.
      const module = namespace && !local.startsWith(`${namespace}/`) ? `${namespace}/${local}` : local;
      return `go get ${module}@${version}`;
    }
    case "nuget":
      return `dotnet add package ${name} --version ${version}`;
    case "maven":
      return `${namespace}:${local}:${version}`;
    case "rpm":
      return `dnf install ${local.split("/").pop()}-${version}`;
    default:
      return "";
  }
}
