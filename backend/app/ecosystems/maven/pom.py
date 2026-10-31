"""POM files and the effective model (SPEC-MULTI-ECOSYSTEM.md §7.4).

A version's dependencies are not in its POM alone. The effective model:

1. the `<parent>` chain, each parent loaded through `load` (the caller's channel: a parent may live in another
   repository of the profile; the documents are cached in source_documents, they are not catalog packages): groupId,
   version, properties, dependencyManagement, dependencies, licenses and links are inherited, the child winning;
2. profiles with `<activeByDefault>true</activeByDefault>` contribute like the POM; the dependencies of a profile
   activated by `<jdk>` are kept with that activation (`Dependency.jdk`) for the resolver to evaluate against the
   environment's `java_version` (§7.3), and when the POM has such profiles its active-by-default dependencies carry
   them in `unless_jdk` (Maven drops active-by-default profiles once another profile of the POM activates); no other
   activation is evaluated, and jdk profiles contribute only dependencies;
3. `${...}` interpolation from `project.*`/`pom.*`, `project.parent.*` and properties (inherited ones included),
   repeated until nothing changes; `env.*`, system properties and anything unknown stay as written and the dependency
   is marked `details.unresolved_property`;
4. `dependencyManagement` with `<scope>import</scope>` BOMs, each BOM's own effective dependencyManagement merged in
   declaration order after the POM's own entries (the first entry for a key wins, as Maven's import does);
5. dependencies without a version or scope take them from dependencyManagement (by groupId:artifactId:type:classifier);
   managed exclusions apply to a dependency that declares none (Maven 3.9's management merger);
6. `<distributionManagement><relocation>` names the artifact's new coordinates.

`<repositories>` are ignored: the profile alone says where artifacts come from (§7.4, point 6).
"""

import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace

MAX_PARENTS = 16
MAX_IMPORT_DEPTH = 8
_PROPERTY = re.compile(r"\$\{([^}]+)\}")

# Common license names to SPDX: POMs write names, not identifiers.
_LICENSES = {
    "apache license, version 2.0": "Apache-2.0", "the apache software license, version 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache-2.0": "Apache-2.0",
    "the apache license, version 2.0": "Apache-2.0", "apache 2": "Apache-2.0", "asl 2.0": "Apache-2.0",
    "mit license": "MIT", "the mit license": "MIT", "mit": "MIT",
    "eclipse public license - v 2.0": "EPL-2.0", "eclipse public license v2.0": "EPL-2.0", "epl-2.0": "EPL-2.0",
    "eclipse public license - v 1.0": "EPL-1.0", "eclipse public license 1.0": "EPL-1.0", "epl-1.0": "EPL-1.0",
    "bsd license": "BSD-3-Clause", "the bsd license": "BSD-3-Clause", "bsd-3-clause": "BSD-3-Clause",
    "bsd 2-clause license": "BSD-2-Clause", "bsd-2-clause": "BSD-2-Clause",
    "gnu lesser general public license": "LGPL-2.1-only", "lgpl-2.1": "LGPL-2.1-only",
    "gnu general public license, version 2 with the classpath exception": "GPL-2.0-only WITH Classpath-exception-2.0",
    "mozilla public license version 2.0": "MPL-2.0", "mpl 2.0": "MPL-2.0", "mpl-2.0": "MPL-2.0",
    "cddl + gplv2 with classpath exception": "CDDL-1.1 OR GPL-2.0-only WITH Classpath-exception-2.0",
}


class PomError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Exclusion:
    group: str
    artifact: str


@dataclass(frozen=True, slots=True)
class Dependency:
    group: str
    artifact: str
    version: str | None
    scope: str | None
    type: str = "jar"
    classifier: str | None = None
    optional: bool = False
    exclusions: tuple[Exclusion, ...] = ()
    unresolved: tuple[str, ...] = ()      # properties left as written
    jdk: str | None = None                # from a profile activated by this `<jdk>` spec
    unless_jdk: tuple[str, ...] = ()      # from an active-by-default profile: dropped when one of these activates

    @property
    def key(self) -> tuple[str, str, str, str | None]:
        return (self.group, self.artifact, self.type, self.classifier)


@dataclass(frozen=True, slots=True)
class Relocation:
    group: str | None
    artifact: str | None
    version: str | None
    message: str | None


@dataclass(slots=True)
class Pom:
    group: str | None
    artifact: str
    version: str | None
    packaging: str = "jar"
    parent: tuple[str, str, str] | None = None   # (group, artifact, version)
    name: str | None = None
    description: str | None = None
    url: str | None = None
    scm_url: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    managed: list[Dependency] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    licenses: list[tuple[str | None, str | None]] = field(default_factory=list)   # (name, url)
    relocation: Relocation | None = None
    developers: list[tuple[str | None, str | None]] = field(default_factory=list)  # (name, email)


@dataclass(frozen=True, slots=True)
class EffectiveModel:
    group: str
    artifact: str
    version: str
    packaging: str
    name: str | None
    description: str | None
    url: str | None
    scm_url: str | None
    dependencies: list[Dependency]
    managed: list[Dependency]
    licenses: list[tuple[str | None, str | None]]
    relocation: Relocation | None
    developers: list[tuple[str | None, str | None]]
    problems: list[str]                    # parents or BOMs that could not be loaded

    @property
    def license_raw(self) -> str | None:
        names = [name or url for name, url in self.licenses if name or url]
        return " OR ".join(n for n in names if n) or None

    @property
    def license_spdx(self) -> str | None:
        spdx = [_LICENSES.get((name or "").strip().lower()) for name, _ in self.licenses]
        if not spdx or any(s is None for s in spdx):
            return None
        return " OR ".join(dict.fromkeys(s for s in spdx if s))


# --- parsing -------------------------------------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _children(element: ElementTree.Element | None, name: str) -> list[ElementTree.Element]:
    return [c for c in element if _local(c.tag) == name] if element is not None else []


def _text(element: ElementTree.Element | None, name: str) -> str | None:
    found = _child(element, name)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _dependencies(container: ElementTree.Element | None) -> list[Dependency]:
    out: list[Dependency] = []
    for dep in _children(_child(container, "dependencies"), "dependency"):
        group, artifact = _text(dep, "groupId"), _text(dep, "artifactId")
        if not group or not artifact:
            continue
        exclusions = tuple(Exclusion(_text(e, "groupId") or "*", _text(e, "artifactId") or "*")
                           for e in _children(_child(dep, "exclusions"), "exclusion"))
        out.append(Dependency(
            group=group, artifact=artifact, version=_text(dep, "version"), scope=_text(dep, "scope"),
            type=_text(dep, "type") or "jar", classifier=_text(dep, "classifier"),
            optional=(_text(dep, "optional") or "").lower() == "true", exclusions=exclusions))
    return out


def _properties(element: ElementTree.Element | None) -> dict[str, str]:
    props = _child(element, "properties")
    return {_local(p.tag): (p.text or "").strip() for p in props} if props is not None else {}


def parse_pom(content: bytes | str) -> Pom:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise PomError(f"not a POM: {exc}") from None
    if _local(root.tag) != "project":
        raise PomError(f"not a POM: the root element is {_local(root.tag)!r}")
    artifact = _text(root, "artifactId")
    if not artifact:
        raise PomError("the POM has no artifactId")
    parent_element = _child(root, "parent")
    parent = None
    if parent_element is not None:
        pg, pa, pv = _text(parent_element, "groupId"), _text(parent_element, "artifactId"), _text(parent_element, "version")
        if pg and pa and pv:
            parent = (pg, pa, pv)
    pom = Pom(
        group=_text(root, "groupId"), artifact=artifact, version=_text(root, "version"),
        packaging=_text(root, "packaging") or "jar", parent=parent, name=_text(root, "name"),
        description=_text(root, "description"), url=_text(root, "url"), scm_url=_text(_child(root, "scm"), "url"),
        properties=_properties(root), managed=_dependencies(_child(root, "dependencyManagement")),
        dependencies=_dependencies(root),
        licenses=[(_text(lic, "name"), _text(lic, "url")) for lic in _children(_child(root, "licenses"), "license")],
        developers=[(_text(d, "name"), _text(d, "email")) for d in _children(_child(root, "developers"), "developer")],
    )
    relocation = _child(_child(root, "distributionManagement"), "relocation")
    if relocation is not None:
        pom.relocation = Relocation(_text(relocation, "groupId"), _text(relocation, "artifactId"),
                                    _text(relocation, "version"), _text(relocation, "message"))
    # Profiles active by default contribute like the POM itself; jdk ones depend on the environment.
    profiles = _children(_child(root, "profiles"), "profile")
    jdk_specs = tuple(spec for p in profiles if (spec := _text(_child(p, "activation"), "jdk")))
    for profile in profiles:
        activation = _child(profile, "activation")
        jdk = _text(activation, "jdk")
        if jdk:
            pom.dependencies += [replace(d, jdk=jdk) for d in _dependencies(profile)]
        elif (_text(activation, "activeByDefault") or "").lower() == "true":
            pom.properties.update(_properties(profile))
            pom.managed += _dependencies(_child(profile, "dependencyManagement"))
            pom.dependencies += [replace(d, unless_jdk=jdk_specs) for d in _dependencies(profile)]
    return pom


# --- the effective model -------------------------------------------------------------------------------------------

Loader = Callable[[str, str, str], Awaitable[Pom | None]]


def _merge_dependencies(child: list[Dependency], parent: list[Dependency]) -> list[Dependency]:
    keys = {d.key for d in child}
    return child + [d for d in parent if d.key not in keys]


def _interpolate(value: str | None, props: Mapping[str, str]) -> tuple[str | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    for _ in range(10):
        new = _PROPERTY.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value, tuple(dict.fromkeys(_PROPERTY.findall(value)))


def _resolve(dep: Dependency, props: Mapping[str, str]) -> Dependency:
    unresolved: list[str] = list(dep.unresolved)
    fields: dict[str, object] = {}
    for name in ("group", "artifact", "version", "scope", "type", "classifier"):
        value, missing = _interpolate(getattr(dep, name), props)
        fields[name] = value
        unresolved += missing
    return replace(dep, **fields, unresolved=tuple(dict.fromkeys(unresolved)))  # type: ignore[arg-type]


async def effective_model(pom: Pom, load: Loader, import_depth: int = 0) -> EffectiveModel:
    problems: list[str] = []
    chain = [pom]
    seen: set[tuple[str, str, str]] = set()
    current = pom
    while current.parent is not None and len(chain) <= MAX_PARENTS:
        if current.parent in seen:
            problems.append(f"parent cycle at {':'.join(current.parent)}")
            break
        seen.add(current.parent)
        parent = await load(*current.parent)
        if parent is None:
            problems.append(f"parent {':'.join(current.parent)} not found")
            break
        chain.append(parent)
        current = parent

    # Inheritance: walk from the topmost parent down, each child overriding.
    group: str | None = None
    version: str | None = None
    props: dict[str, str] = {}
    managed: list[Dependency] = []
    dependencies: list[Dependency] = []
    licenses: list[tuple[str | None, str | None]] = []
    developers: list[tuple[str | None, str | None]] = []
    url = scm_url = None
    for item in reversed(chain):
        group = item.group or (item.parent[0] if item.parent else None) or group
        version = item.version or (item.parent[2] if item.parent else None) or version
        props.update(item.properties)
        managed = _merge_dependencies(item.managed, managed)
        dependencies = _merge_dependencies(item.dependencies, dependencies)
        licenses = item.licenses or licenses
        developers = item.developers or developers
        url = item.url or url
        scm_url = item.scm_url or scm_url
    if group is None or version is None:
        raise PomError(f"{pom.artifact}: no groupId or version even after the parents")

    project = {"groupId": group, "artifactId": pom.artifact, "version": version, "packaging": pom.packaging}
    for key, value in project.items():
        props.setdefault(f"project.{key}", value)
        props.setdefault(f"pom.{key}", value)
    props.setdefault("version", version)
    if pom.parent is not None:
        props.setdefault("project.parent.groupId", pom.parent[0])
        props.setdefault("project.parent.artifactId", pom.parent[1])
        props.setdefault("project.parent.version", pom.parent[2])
    props = {key: _interpolate(value, props)[0] or "" for key, value in props.items()}

    managed = [_resolve(d, props) for d in managed]
    managed = await _import_boms(managed, load, problems, depth=import_depth)
    by_key: dict[tuple[str, str, str, str | None], Dependency] = {}
    for entry in managed:
        by_key.setdefault(entry.key, entry)

    resolved: list[Dependency] = []
    for dep in dependencies:
        dep = _resolve(dep, props)
        managed_entry = by_key.get(dep.key)
        if managed_entry is not None:
            dep = replace(dep, version=dep.version or managed_entry.version, scope=dep.scope or managed_entry.scope,
                          exclusions=dep.exclusions or managed_entry.exclusions)
        resolved.append(dep)
    relocation = pom.relocation
    return EffectiveModel(
        group=group, artifact=pom.artifact, version=version, packaging=pom.packaging,
        name=pom.name, description=pom.description, url=url, scm_url=scm_url, dependencies=resolved,
        managed=[d for d in managed if d.scope != "import"], licenses=licenses, relocation=relocation,
        developers=developers, problems=problems)


async def _import_boms(managed: list[Dependency], load: Loader, problems: list[str], depth: int) -> list[Dependency]:
    """`import`-scoped pom entries replaced by the BOM's effective dependencyManagement, after the POM's own entries."""
    own = [d for d in managed if not (d.scope == "import" and d.type == "pom")]
    imported: list[Dependency] = []
    for bom in (d for d in managed if d.scope == "import" and d.type == "pom"):
        if depth >= MAX_IMPORT_DEPTH or bom.version is None or bom.unresolved:
            problems.append(f"BOM {bom.group}:{bom.artifact}:{bom.version} cannot be imported")
            continue
        pom = await load(bom.group, bom.artifact, bom.version)
        if pom is None:
            problems.append(f"BOM {bom.group}:{bom.artifact}:{bom.version} not found")
            continue
        model = await effective_model(pom, load, depth + 1)
        problems += model.problems
        imported += model.managed
    return own + imported
