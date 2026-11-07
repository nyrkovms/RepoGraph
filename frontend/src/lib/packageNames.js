export const ECOSYSTEM_OPTIONS = [
  { value: "", label: "All" },
  { value: "pypi", label: "PyPI" },
  { value: "npm", label: "npm" },
  { value: "crates", label: "crates.io" },
  { value: "go", label: "Go" },
  { value: "nuget", label: "NuGet" },
  { value: "maven", label: "Maven" },
  { value: "rpm", label: "RPM" },
];

export function ecosystemLabel(ecosystem) {
  return ECOSYSTEM_OPTIONS.find((option) => option.value === ecosystem)?.label || ecosystem;
}

// The native spelling of the package name: an npm scope is kept apart from the name in the catalog (`@angular` + `core`),
// a Maven groupId too (`org.springframework` + `spring-core`), an RPM distro too (`rocky-9` + `openssl`).
export function nativeName(item) {
  if (item.ecosystem === "maven") {
    return `${item.namespace}:${item.normalized_name}`;
  }
  if (item.ecosystem === "rpm") {
    return `${item.namespace}/${item.normalized_name}`;
  }
  return item.namespace ? `@${item.namespace}/${item.normalized_name}` : item.normalized_name;
}

export function displayName(item) {
  // The name of a Maven package already is `groupId:artifactId`, that of an RPM package `distro/name`.
  if (item.ecosystem === "maven" || item.ecosystem === "rpm") {
    return item.name;
  }
  return item.namespace ? `@${item.namespace}/${item.name}` : item.name;
}

export function packageHref(item) {
  return `/package/${item.ecosystem}/${nativeName(item)}`;
}

export function itemKey(item) {
  return `${item.ecosystem}-${nativeName(item)}-${item.latest_version}`;
}
