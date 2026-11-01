"""Maven resolve: the artifacts `mvn dependency:list` lists for a project depending on the roots
(SPEC-MULTI-ECOSYSTEM.md §9.7).

`strategy=maven` follows Maven 3.9 (Maven Resolver's collector, `NearestVersionSelector`, `JavaScopeSelector`):

1. The graph is walked breadth-first from the project's dependencies (the roots), each version's dependencies taken
   from its effective model (the POM with parents and BOMs, built by the adapter).
2. **Nearest wins**: a groupId:artifactId is decided by its nearest occurrence; at one depth the first one wins. Other
   occurrences do not change the version and their subtrees are not walked.
3. The project's dependencyManagement (`managed`; an entry with `type=pom` is a BOM whose effective
   dependencyManagement is imported after the explicit entries, in order, read through the catalog's document
   channel) sets the version and scope of every transitive occurrence of an artifact it names
   (`ClassicDependencyManager`: depth 2 and below); a managed scope is kept as set. The roots keep theirs, except a
   root without a version (`*`), which takes the managed one as a versionless project dependency does.
4. A bare version is a soft requirement taken as written; a range takes the highest version available in it; a root
   asked for with `*` and not managed takes the highest release. A range is a hard constraint wherever it is reached
   (`NearestVersionSelector`): an occurrence whose version some range of the same artifact rejects loses, and the
   nearest acceptable one wins (a range: its highest version all ranges accept). The ranges count only below the
   winners, so the walk is repeated with the ranges the last one reached until they stay the same; when no
   occurrence is acceptable the resolve fails with `resolver_conflict`, as Maven does.
5. A relocated artifact (its POM's `<relocation>`, a `details.relocation` dependency) is replaced by its target in
   the same place, as Maven's descriptor reader does; Gradle follows it as a dependency.
6. Profiles activated by `<jdk>` apply when `environment.java_version` activates them.
7. A dependency's `test`, `provided` and `system` dependencies and its `optional` ones are not followed; exclusions
   accumulate down a path.
8. Scopes are settled after the walk: a node's scope is derived from each parent's scope (`JavaScopeDeriver`) and the
   widest of them wins (compile, runtime, provided, test), a root keeping its own (`JavaScopeSelector`).

`strategy=gradle` follows Gradle 8 reading POMs only (Gradle Module Metadata is not read, Q15; a chosen version that
has it gets a warning):

1. The **highest** version any reached module asks for wins, wherever it is asked; a range counts with its highest
   version only when no fixed version asked for lies in it; `managed` entries (a `type=pom` one is a platform: its
   dependencyManagement) are constraints: they take part in that choice for a module that is reached, and add none.
   The dependencyManagement of a dependency's POM only fills its own versions, as in Maven. Versions are ordered by
   ComparableVersion, which differs from Gradle's order in corner cases (`1.0` and `1.0.0` are equal here).
2. The graph is walked again with the chosen versions until nothing changes, so the dependencies of a version that
   lost no longer count.
3. A module is excluded only when every path to it excludes it.
4. Plugins in `environment.plugins` (`<id>:<version>`) are roots by their marker artifact `<id>:<id>.gradle.plugin`.

The graph lists the artifacts whose scope is one of `environment.scopes`.
"""

import re
import time
from collections import deque
from collections.abc import Callable, Sequence
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
from app.ecosystems.maven.environment import MavenEnvironment, jdk_applies, maven_scope
from app.ecosystems.maven.pom import Pom, PomError, effective_model, parse_pom
from app.ecosystems.maven.versions import MavenVersionScheme

UNKNOWN_DEPENDENCIES = "dependencies unknown"
MAX_GRADLE_PASSES = 20
MAX_MAVEN_PASSES = 20
MAX_RELOCATIONS = 16
MAX_PATH_EXCLUSIONS = 8
# Gradle writes this into the POM of everything it publishes together with a `.module` file.
_GRADLE_MARKER = re.compile(rb"<!--\s*do_not_remove:\s*published-with-gradle-metadata\s*-->")
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
    managed: bool = False               # the scope is the managed one: kept as set, not derived


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


def _is_range(raw: str) -> bool:
    try:
        return raw not in ("", "*") and _SCHEME.constraint(raw).recommended is None
    except InvalidVersion:
        return False


def _accepted(version: str, ranges: Sequence[str] | frozenset[str]) -> bool:
    return all(_SCHEME.constraint(raw).contains(version) for raw in ranges)


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
        self.relocated: dict[GA, GA] = {}
        self.scopes: dict[GA, str] = {}
        self.poms: dict[tuple[str, str, str], bytes] = {}
        self.root_list = list(request.roots) + [
            ResolveRoot(ref=PackageRef("maven", plugin_id, f"{plugin_id}.gradle.plugin"), constraint=version)
            for plugin_id, version in self.env.plugins]

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

    async def pick(self, ref: PackageRef, raw: str, ranges: Sequence[str] = ()) -> CatalogVersion | None:
        """The version `raw` asks for; with `ranges`, only a version every one of them accepts."""
        versions = [v for v in await self.candidates(ref) if _accepted(v.version, ranges)] if ranges \
            else await self.candidates(ref)
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
        """The version asked for: the project's dependencyManagement wins below the roots, and gives a versionless
        root its version."""
        managed = self.managed.get(occurrence.ga)
        if managed is None or not managed.constraint:
            return occurrence.raw
        if occurrence.parent is None and occurrence.raw not in ("", "*"):
            return occurrence.raw
        return managed.constraint

    def _target(self, ga: GA) -> GA:
        for _ in range(MAX_RELOCATIONS):
            if ga not in self.relocated:
                break
            ga = self.relocated[ga]
        return ga

    def _transitive(self, deps: Sequence[NormalizedDependency] | None, exclusions: frozenset[GA],
                    excluded: Callable[[GA], bool]) -> list[tuple[NormalizedDependency, _Child, frozenset[GA]]]:
        """The dependencies a version passes on, with the scope they arrive with and the exclusions below them."""
        out = []
        for dep in deps or ():
            child, scope = _ga(dep.target), maven_scope(dep)
            if dep.dependency_type == "optional" or scope in _NOT_TRANSITIVE or excluded(child):
                continue
            if not jdk_applies(dep, self.env.java_version):
                continue
            managed = self.managed.get(child)
            managed_scope = managed.qualifiers.get("scope") if managed is not None else None
            out.append((dep, _Child(child, managed_scope or scope, dep.constraint, managed_scope is not None),
                        exclusions | _exclusions(dep)))
        return out

    async def _pom(self, group: str, artifact: str, version: str) -> Pom | None:
        ref = PackageRef("maven", group, artifact)
        try:
            document = await self.catalog.document(ref, pom_path(ref, version), self.request.profile)
            if document is None:
                return None
            self.poms[(group, artifact, version)] = document.content
            return parse_pom(document.content)
        except (PomError, ValueError):
            return None

    async def import_managed(self) -> None:
        # Maven imports BOMs after the project's own entries, whatever their order: an explicit entry always wins.
        for entry in self.request.managed:
            if entry.qualifiers.get("type") != "pom":
                self.managed.setdefault(_ga(entry.ref), entry)
        for entry in self.request.managed:
            if entry.qualifiers.get("type") != "pom":
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
        missing_boms = list(self.unresolvable)
        ranges: dict[GA, frozenset[str]] = {}
        for _ in range(MAX_MAVEN_PASSES):
            self.unresolvable = list(missing_boms)
            reached, rejected = await self._maven_pass(ranges)
            # Ranges only added and every winner acceptable to them: another pass would choose the same.
            if all(ranges.get(ga, frozenset()) <= reached.get(ga, frozenset()) for ga in ranges) \
                    and all(_accepted(node.version.version, reached.get(ga, ())) for ga, node in self.chosen.items()):
                conflicts = [ga for ga in rejected if ga not in self.chosen]
                if conflicts:
                    raise _Aborted("resolver_conflict", "; ".join(
                        f"could not resolve version conflict among [{', '.join(rejected[ga])}]" for ga in conflicts))
                return
            ranges = reached
        self.graph.warnings.append({"code": "not_settled",
                                    "message": f"versions still changed after {MAX_MAVEN_PASSES} passes"})

    async def _maven_pass(self, ranges: dict[GA, frozenset[str]]) -> tuple[dict[GA, frozenset[str]], dict[GA, list[str]]]:
        """One walk with the ranges known so far as hard constraints: the ranges it reaches below its winners, and
        the occurrences a range rejected."""
        self.chosen, self.relocated, self.roots, self.errors = {}, {}, {}, {}
        reached: dict[GA, set[str]] = {}
        rejected: dict[GA, list[str]] = {}
        queue: deque[_Occurrence] = deque()
        for order, root in enumerate(self.root_list):
            self.roots.setdefault(_ga(root.ref), root)
            queue.append(_Occurrence(_ga(root.ref), root.ref, root.constraint, 1, None, frozenset(), order))
        order = len(queue)
        while queue:
            self._check()
            occurrence = queue.popleft()
            ga = occurrence.ga
            raw = self._requested(occurrence)
            if _is_range(raw):
                reached.setdefault(ga, set()).add(raw)
            if ga in self.chosen:
                continue                                                      # nearest (or first) already won
            found = await self.pick(occurrence.ref, raw, tuple(ranges.get(ga, ())))
            if found is None:
                if ranges.get(ga) and await self.pick(occurrence.ref, raw) is not None:
                    # A range rejects it, so a farther occurrence may win; Maven's message names each one.
                    parent = self.chosen.get(occurrence.parent) if occurrence.parent else None
                    path = f"{parent.occurrence.ga[0]}:{parent.occurrence.ga[1]}:{parent.version.version} -> " \
                        if parent else ""
                    rejected.setdefault(ga, []).append(f"{path}{ga[0]}:{ga[1]}:{raw}")
                else:
                    self._unresolvable(occurrence, raw)
                continue
            if len(self.chosen) >= self.request.max_nodes:
                raise ResolveLimitExceeded("nodes", self.request.max_nodes)
            deps = await self.catalog.dependencies(occurrence.ref, found.version, found.registry_id)
            relocation = next((d for d in deps or () if (d.details or {}).get("relocation")), None)
            if relocation is not None and len(self.relocated) < MAX_RELOCATIONS:
                # The target takes the artifact's place: same depth, parent, order and exclusions.
                target = _ga(relocation.target)
                self.relocated[ga] = target
                if occurrence.parent is None:
                    self.roots.setdefault(target, ResolveRoot(ref=relocation.target, constraint=relocation.constraint))
                queue.appendleft(_Occurrence(target, relocation.target, relocation.constraint, occurrence.depth,
                                             occurrence.parent, occurrence.exclusions, occurrence.order))
                continue
            chosen = _Chosen(found, occurrence)
            self.chosen[ga] = chosen
            if deps is None:
                self._error(ga, UNKNOWN_DEPENDENCIES)
                continue
            for dep, child, below in self._transitive(deps, occurrence.exclusions,
                                                       lambda g: _excluded(g, occurrence.exclusions)):
                chosen.children.append(child)
                queue.append(_Occurrence(child.ga, dep.target, dep.constraint, occurrence.depth + 1, ga, below, order))
                order += 1
        return {ga: frozenset(asked) for ga, asked in reached.items()}, rejected

    def _unresolvable(self, occurrence: _Occurrence, raw: str) -> None:
        ga = occurrence.ga
        where = f" (required by {occurrence.parent[0]}:{occurrence.parent[1]})" if occurrence.parent else ""
        message = f"no version of {ga[0]}:{ga[1]} matches {raw}{where}"
        if message not in self.unresolvable:
            self.unresolvable.append(message)

    async def walk_gradle(self) -> None:
        await self.import_managed()
        for root in self.root_list:
            self.roots.setdefault(_ga(root.ref), root)
        selected: dict[GA, CatalogVersion] = {}
        exclusions: dict[GA, frozenset[frozenset[GA]]] = {}
        for _ in range(MAX_GRADLE_PASSES):
            chosen, requests, reached_exclusions = await self._gradle_pass(selected, exclusions)
            settled = {ga: await self._highest(node, requests.get(ga, [])) for ga, node in chosen.items()}
            if {ga: v.version for ga, v in settled.items()} == {ga: v.version for ga, v in selected.items()} \
                    and reached_exclusions == exclusions:
                self.chosen = chosen
                return
            selected, exclusions = settled, reached_exclusions
        self.chosen = chosen
        self.graph.warnings.append({"code": "not_settled",
                                    "message": f"versions still changed after {MAX_GRADLE_PASSES} passes"})

    async def _highest(self, node: _Chosen, requests: list[str]) -> CatalogVersion:
        """Gradle's choice among what is asked: the highest; a range only counts when no fixed version lies in it."""
        fixed: list[CatalogVersion] = []
        ranges: list[str] = []
        for raw in requests:
            try:
                is_range = raw != "*" and _SCHEME.constraint(raw).recommended is None
            except InvalidVersion:
                continue
            if is_range:
                ranges.append(raw)
            elif (found := await self.pick(node.occurrence.ref, raw)) is not None:
                fixed.append(found)
        for raw in ranges:
            if not any(_SCHEME.constraint(raw).contains(v.version) for v in fixed):
                if (found := await self.pick(node.occurrence.ref, raw)) is not None:
                    fixed.append(found)
        return max(fixed or [node.version], key=lambda v: _SCHEME.sort_key(v.version))

    async def _gradle_pass(
        self, selected: dict[GA, CatalogVersion], exclusions: dict[GA, frozenset[frozenset[GA]]],
    ) -> tuple[dict[GA, _Chosen], dict[GA, list[str]], dict[GA, frozenset[frozenset[GA]]]]:
        """One walk with the versions chosen so far: what each reached module asks for, and the exclusions of every
        path to a module (a dependency is excluded only when every path's exclusions exclude it)."""
        self.errors, self.unresolvable = {}, []
        chosen: dict[GA, _Chosen] = {}
        requests: dict[GA, list[str]] = {}
        reached: dict[GA, set[frozenset[GA]]] = {}
        queue: deque[_Occurrence] = deque()
        for order, root in enumerate(self.root_list):
            occurrence = _Occurrence(_ga(root.ref), root.ref, root.constraint, 1, None, frozenset(), order)
            occurrence.raw = self._requested(occurrence)                    # a versionless root: the managed one
            queue.append(occurrence)
            requests.setdefault(_ga(root.ref), []).append(occurrence.raw)
            reached[_ga(root.ref)] = {frozenset()}
        order = len(queue)
        while queue:
            self._check()
            occurrence = queue.popleft()
            ga = occurrence.ga
            if ga in chosen:
                continue
            version = selected.get(ga) or await self.pick(occurrence.ref, occurrence.raw)
            if version is None:
                self._unresolvable(occurrence, occurrence.raw)
                continue
            if len(chosen) >= self.request.max_nodes:
                raise ResolveLimitExceeded("nodes", self.request.max_nodes)
            node = chosen[ga] = _Chosen(version, occurrence)
            paths = exclusions.get(ga) or frozenset({occurrence.exclusions})
            deps = await self.catalog.dependencies(occurrence.ref, version.version, version.registry_id)
            if deps is None:
                self._error(ga, UNKNOWN_DEPENDENCIES)
            for dep, child, _ in self._transitive(deps, frozenset(),
                                                  lambda g: all(_excluded(g, path) for path in paths)):
                node.children.append(child)
                requests.setdefault(child.ga, []).append(child.raw)
                below = {path | _exclusions(dep) for path in paths}
                reached.setdefault(child.ga, set()).update(below)
                queue.append(_Occurrence(child.ga, dep.target, child.raw, occurrence.depth + 1, ga,
                                         min(below, key=len), order))
                order += 1
        # Constraints count only for modules something depends on.
        for ga, managed in self.managed.items():
            if ga in chosen and managed.constraint:
                requests[ga].append(managed.constraint)
        # Few distinct exclusion sets per module in practice; the smallest ones decide, so a bound keeps it finite.
        return chosen, requests, {ga: frozenset(sorted(sets, key=len)[:MAX_PATH_EXCLUSIONS]) for ga, sets in reached.items()}

    async def warn_gradle_metadata(self) -> None:
        for ga, node in self.chosen.items():
            if self.scopes.get(ga) not in self.env.scopes:
                continue
            await self._pom(ga[0], ga[1], node.version.version)          # kept by the catalog: read once
            if _GRADLE_MARKER.search(self.poms.get((ga[0], ga[1], node.version.version), b"")):
                self.graph.warnings.append({
                    "code": "gradle_module_metadata", "package": f"{ga[0]}:{ga[1]}", "version": node.version.version,
                    "message": "resolved from the POM: the Gradle Module Metadata published with it is not read"})

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
                    target = self._target(child.ga)
                    if target in self.chosen and target not in self.roots:
                        # Maven keeps a managed scope as set (ConflictResolver's managed-scope rule).
                        scope = child.scope if child.managed else derived_scope(parent_scope, child.scope)
                        derived.setdefault(target, set()).add(scope)
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
                target = self._target(child.ga)
                if target in ids and target not in seen:
                    seen.add(target)
                    self.graph.edges.append(ResolvedEdge(source=ids[ga], target=ids[target], constraint=child.raw,
                                                         dependency_type="runtime", details={"scope": child.scope}))
        for message in self.unresolvable:
            self.graph.warnings.append({"code": "unresolvable", "message": message})


class MavenResolver:
    emulates = "Maven 3.9"
    strategy = "maven-nearest-wins"

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            await (state.walk_gradle() if state.gradle else state.walk())
            missing = [f"{r.ref.namespace}:{r.ref.name}" for r in state.root_list if not state.versions.get(_ga(r.ref))]
            if missing:
                state.graph.error = {"code": "not_found", "packages": missing,
                                     "message": f"no source of the profile has {', '.join(missing)}"}
                return state.graph
            state.settle_scopes()
            if state.gradle:
                await state.warn_gradle_metadata()
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
