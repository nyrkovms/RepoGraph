"""Maven resolve: the artifacts `mvn dependency:list` lists for a project depending on the roots
(SPEC-MULTI-ECOSYSTEM.md §9.7).

`strategy=maven` follows Maven 3.9 (Maven Resolver's collector, `NearestVersionSelector`, `JavaScopeSelector`):

1. The graph is walked breadth-first from the project's dependencies (the roots), each version's dependencies taken
   from its effective model (the POM with parents and BOMs, built by the adapter).
2. **Nearest wins**: a groupId:artifactId is decided by its nearest occurrence; at one depth the first one wins. Other
   occurrences do not change the version and their subtrees are not walked.
3. The project's dependencyManagement (`managed`; an entry with `type=pom` is a BOM whose effective
   dependencyManagement is imported in its place, read through the catalog's document channel) sets the version and
   scope of every transitive occurrence of an artifact it names (`ClassicDependencyManager`: depth 2 and below); the
   roots keep theirs. The first entry for an artifact wins, as in Maven.
4. A bare version is a soft requirement taken as written; a range takes the highest version available in it; a root
   asked for with `*` takes the highest release.
5. A dependency's `test`, `provided` and `system` dependencies and its `optional` ones are not followed; exclusions
   accumulate down a path.
6. Scopes are settled after the walk: a node's scope is derived from each parent's scope (`JavaScopeDeriver`) and the
   widest of them wins (compile, runtime, provided, test), a root keeping its own (`JavaScopeSelector`).

`strategy=gradle` takes the same walk but the **highest** version of an artifact wins wherever it is declared, and
`managed` entries act as constraints (they raise, never lower). The graph lists the artifacts whose scope is one of
`environment.scopes`.
"""

import time
from collections import deque
from dataclasses import dataclass, field

from app.ecosystems.base import (
    CatalogAccess,
    CatalogVersion,
    InvalidVersion,
    NormalizedDependency,
    PackageRef,
    ResolvedEdge,
    ResolvedNode,
    ResolveGraph,
    ResolveLimitExceeded,
    ResolveRequest,
    ResolveRoot,
)
from app.ecosystems.maven.coordinates import pom_path
from app.ecosystems.maven.environment import MavenEnvironment, maven_scope
from app.ecosystems.maven.pom import Pom, PomError, effective_model, parse_pom
from app.ecosystems.maven.versions import MavenVersionScheme

UNKNOWN_DEPENDENCIES = "dependencies unknown"
_SCHEME = MavenVersionScheme()
_NOT_TRANSITIVE = ("test", "provided", "system")
_WIDTH = {"compile": 4, "runtime": 3, "provided": 2, "test": 1, "system": 0}
GA = tuple[str, str]


def derived_scope(parent: str, child: str) -> str:
    """`JavaScopeDeriver.getDerivedScope`: the scope a dependency of the given scope gets below a parent."""
    if child in ("system", "test") or parent == "compile":
        return child
    if parent in ("test", "runtime"):
        return parent
    if parent in ("system", "provided"):
        return "provided"
    return "runtime"


def widest(scopes: set[str]) -> str:
    """`JavaScopeSelector.chooseEffectiveScope`: system only when alone, else the widest."""
    if len(scopes) > 1:
        scopes = scopes - {"system"}
    return max(scopes, key=lambda s: _WIDTH.get(s, -1))


@dataclass(slots=True)
class _Occurrence:
    ga: GA
    ref: PackageRef
    raw: str                            # the version or range as declared
    depth: int
    parent: GA | None                   # None: the project
    exclusions: frozenset[GA]
    order: int


@dataclass(slots=True)
class _Child:
    ga: GA
    scope: str                          # as declared, or as the project's dependencyManagement sets it
    raw: str


@dataclass(slots=True)
class _Chosen:
    version: CatalogVersion
    occurrence: _Occurrence
    children: list[_Child] = field(default_factory=list)


class _Aborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ga(ref: PackageRef) -> GA:
    return (ref.namespace, ref.name)


def _exclusions(dep: NormalizedDependency) -> frozenset[GA]:
    listed = (dep.details or {}).get("exclusions")
    out: set[GA] = set()
    for item in listed if isinstance(listed, list) else []:
        if isinstance(item, dict):
            out.add((str(item.get("group", "*")), str(item.get("artifact", "*"))))
    return frozenset(out)


def _excluded(ga: GA, exclusions: frozenset[GA]) -> bool:
    return any(g in ("*", ga[0]) and a in ("*", ga[1]) for g, a in exclusions)


class _Resolve:
    def __init__(self, request: ResolveRequest, catalog: CatalogAccess) -> None:
        self.request = request
        self.catalog = catalog
        environment = request.environment
        self.env = environment if isinstance(environment, MavenEnvironment) else MavenEnvironment()
        self.gradle = (request.strategy or self.env.strategy) == "gradle"
        self.deadline = time.monotonic() + request.timeout_s
        self.graph = ResolveGraph(strategy="gradle-highest-wins" if self.gradle else "maven-nearest-wins",
                                  nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        self.versions: dict[GA, list[CatalogVersion]] = {}
        self.fetches = 0
        self.errors: dict[GA, list[str]] = {}
        self.unresolvable: list[str] = []
        self.managed: dict[GA, ResolveRoot] = {}
        self.roots: dict[GA, ResolveRoot] = {}
        self.chosen: dict[GA, _Chosen] = {}
        self.scopes: dict[GA, str] = {}

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s")

    async def candidates(self, ref: PackageRef) -> list[CatalogVersion]:
        ga = _ga(ref)
        if ga not in self.versions:
            self._check()
            versions = list(await self.catalog.versions(ref, self.request.profile))
            if not versions:
                if self.fetches >= self.request.max_lazy_fetches:
                    raise ResolveLimitExceeded("lazy_fetches", self.request.max_lazy_fetches)
                self.fetches += 1
                if await self.catalog.ensure(ref, self.request.profile):
                    self.graph.fetched.append(ref)
                    versions = list(await self.catalog.versions(ref, self.request.profile))
            self.versions[ga] = versions
        return self.versions[ga]

    async def pick(self, ref: PackageRef, raw: str) -> CatalogVersion | None:
        versions = await self.candidates(ref)
        if raw == "*":
            releases = [v for v in versions if v.status == "active" and not _SCHEME.is_prerelease(v.version)]
            return max(releases or versions, key=lambda v: _SCHEME.sort_key(v.version), default=None)
        try:
            constraint = _SCHEME.constraint(raw)
        except InvalidVersion:
            return None
        if constraint.recommended is not None:
            # A soft requirement is used as written, as Maven does, when the repository has it.
            return next((v for v in versions if v.version == constraint.recommended), None)
        best = constraint.best_match([v.version for v in versions])
        return next((v for v in versions if v.version == best), None) if best else None

    def _error(self, ga: GA, message: str) -> None:
        messages = self.errors.setdefault(ga, [])
        if message not in messages:
            messages.append(message)

    def _requested(self, occurrence: _Occurrence) -> str:
        """The version asked for: the project's dependencyManagement wins below the roots (as a Gradle constraint it
        only raises, which the highest-wins walk does by itself once the managed version is seen)."""
        managed = self.managed.get(occurrence.ga) if occurrence.parent is not None else None
        if managed is None or not managed.constraint or self.gradle:
            return occurrence.raw
        return managed.constraint

    async def _pom(self, group: str, artifact: str, version: str) -> Pom | None:
        ref = PackageRef("maven", group, artifact)
        try:
            document = await self.catalog.document(ref, pom_path(ref, version), self.request.profile)
            return parse_pom(document.content) if document is not None else None
        except (PomError, ValueError):
            return None

    async def import_managed(self) -> None:
        for entry in self.request.managed:
            if entry.qualifiers.get("type") != "pom":
                self.managed.setdefault(_ga(entry.ref), entry)
                continue
            ga = _ga(entry.ref)
            bom = await self._pom(ga[0], ga[1], entry.constraint)
            if bom is None:
                self.unresolvable.append(f"BOM {ga[0]}:{ga[1]}:{entry.constraint} not found")
                continue
            model = await effective_model(bom, self._pom)
            for dep in model.managed:
                if dep.version and dep.scope != "import":
                    self.managed.setdefault((dep.group, dep.artifact), ResolveRoot(
                        ref=PackageRef("maven", dep.group, dep.artifact), constraint=dep.version,
                        qualifiers={"scope": dep.scope} if dep.scope else {}))

    async def walk(self) -> None:
        await self.import_managed()
        queue: deque[_Occurrence] = deque()
        for order, root in enumerate(self.request.roots):
            self.roots.setdefault(_ga(root.ref), root)
            queue.append(_Occurrence(_ga(root.ref), root.ref, root.constraint, 1, None, frozenset(), order))
        order = len(queue)
        while queue:
            self._check()
            occurrence = queue.popleft()
            ga = occurrence.ga
            existing = self.chosen.get(ga)
            if existing is not None and not self.gradle:
                continue                                                      # nearest (or first) already won
            raw = self._requested(occurrence)
            found = await self.pick(occurrence.ref, raw)
            if found is None:
                if existing is None:
                    where = f" (required by {occurrence.parent[0]}:{occurrence.parent[1]})" if occurrence.parent else ""
                    self.unresolvable.append(f"no version of {ga[0]}:{ga[1]} matches {raw}{where}")
                continue
            if existing is not None:
                if _SCHEME.sort_key(found.version) <= _SCHEME.sort_key(existing.version.version):
                    continue
                occurrence = existing.occurrence                              # gradle: the higher version, same place
            elif len(self.chosen) >= self.request.max_nodes:
                raise ResolveLimitExceeded("nodes", self.request.max_nodes)
            chosen = _Chosen(found, occurrence)
            self.chosen[ga] = chosen
            deps = await self.catalog.dependencies(occurrence.ref, found.version, found.registry_id)
            if deps is None:
                self._error(ga, UNKNOWN_DEPENDENCIES)
                continue
            for dep in deps:
                child = _ga(dep.target)
                scope = maven_scope(dep)
                if dep.dependency_type == "optional" or scope in _NOT_TRANSITIVE:
                    continue
                if _excluded(child, occurrence.exclusions):
                    continue
                managed = self.managed.get(child)
                if managed is not None and managed.qualifiers.get("scope"):
                    scope = managed.qualifiers["scope"]
                chosen.children.append(_Child(child, scope, dep.constraint))
                queue.append(_Occurrence(child, dep.target, dep.constraint, occurrence.depth + 1, ga,
                                         occurrence.exclusions | _exclusions(dep), order))
                order += 1

    def settle_scopes(self) -> None:
        """Scopes once the versions are chosen: roots keep theirs, the rest take the widest derived one. Widening a
        parent can only widen its children, so repeating until nothing changes ends."""
        self.scopes = {ga: root.qualifiers.get("scope") or "compile" for ga, root in self.roots.items()
                       if ga in self.chosen}
        changed = True
        while changed:
            changed = False
            derived: dict[GA, set[str]] = {}
            for parent, chosen in self.chosen.items():
                parent_scope = self.scopes.get(parent)
                if parent_scope is None:
                    continue
                for child in chosen.children:
                    if child.ga in self.chosen and child.ga not in self.roots:
                        derived.setdefault(child.ga, set()).add(derived_scope(parent_scope, child.scope))
            for ga, scopes in derived.items():
                scope = widest(scopes)
                if self.scopes.get(ga) != scope:
                    self.scopes[ga] = scope
                    changed = True

    def build(self) -> None:
        listed = {ga: chosen for ga, chosen in self.chosen.items() if self.scopes.get(ga) in self.env.scopes}
        ordered = sorted(listed, key=lambda g: (listed[g].occurrence.depth, listed[g].occurrence.order))
        ids = {ga: f"n{number}" for number, ga in enumerate(ordered, start=1)}
        for ga in ordered:
            chosen = listed[ga]
            latest = None
            if ga in self.roots:
                releases = [v.version for v in self.versions.get(ga, [])
                            if v.status == "active" and not _SCHEME.is_prerelease(v.version)]
                latest = max(releases, key=_SCHEME.sort_key) if releases else None
            self.graph.nodes.append(ResolvedNode(
                id=ids[ga], ref=chosen.occurrence.ref, version=chosen.version.version,
                registry_slug=chosen.version.registry_slug, depth=chosen.occurrence.depth,
                errors=list(self.errors.get(ga, [])), latest_compatible=latest, details={"scope": self.scopes[ga]}))
        for ga in ordered:
            if ga in self.roots:
                self.graph.edges.append(ResolvedEdge(source="root", target=ids[ga],
                                                     constraint=self.roots[ga].constraint, dependency_type="runtime"))
        for ga in ordered:
            seen: set[GA] = set()
            for child in listed[ga].children:
                if child.ga in ids and child.ga not in seen:
                    seen.add(child.ga)
                    self.graph.edges.append(ResolvedEdge(source=ids[ga], target=ids[child.ga], constraint=child.raw,
                                                         dependency_type="runtime", details={"scope": child.scope}))
        for message in self.unresolvable:
            self.graph.warnings.append({"code": "unresolvable", "message": message})


class MavenResolver:
    emulates = "Maven 3.9"
    strategy = "maven-nearest-wins"

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            await state.walk()
            missing = [f"{r.ref.namespace}:{r.ref.name}" for r in request.roots if not state.versions.get(_ga(r.ref))]
            if missing:
                state.graph.error = {"code": "not_found", "packages": missing,
                                     "message": f"no source of the profile has {', '.join(missing)}"}
                return state.graph
            state.settle_scopes()
            state.build()
        except ResolveLimitExceeded as exc:
            state.graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit,
                                 "value": exc.value, "packages_seen": len(state.versions), "lazy_fetches": state.fetches}
        except _Aborted as exc:
            state.graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(state.versions)}
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            state.graph.error = {"code": "upstream_error", "message": f"{type(exc).__name__}: {exc}",
                                 "packages_seen": len(state.versions)}
        return state.graph


RESOLVER = MavenResolver()
