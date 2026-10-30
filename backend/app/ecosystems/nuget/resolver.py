"""NuGet resolve: the packages `dotnet restore` puts in project.assets.json (SPEC-MULTI-ECOSYSTEM.md §9.6).

The graph is walked level by level from the project's references, each version's dependencies taken from the group
its target framework picks (frameworks.py):

1. **Lowest applicable version** — a range takes the lowest version it allows (a floating one the highest matching the
   float), as `VersionRange.FindBestMatch`; listed versions first, an unlisted one only when nothing listed fits.
2. **Direct dependency wins** — a package is decided at the level nearest to the project; references further down
   are not used for it (one that wanted a higher version is a downgrade, NU1605, reported as a warning).
3. **Cousin dependencies** — references to one package at the same level take the lowest version all their ranges
   allow; when there is none the restore fails (NU1107).

Only the decided version's dependencies are walked, so a package reachable only through a version that lost is not
in the graph.
"""

import time
from collections import defaultdict

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
)
from app.ecosystems.nuget.environment import NuGetEnvironment, dependency_applies
from app.ecosystems.nuget.versions import NuGetConstraint, NuGetVersionScheme, parse_range

STRATEGY = "nuget-nearest-wins"
UNKNOWN_DEPENDENCIES = "dependencies unknown"
_SCHEME = NuGetVersionScheme()


class _Aborted(Exception):
    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _range(raw: str) -> NuGetConstraint:
    # A dependency without a range takes any version, the lowest first (`[0.0.0, )`).
    return parse_range(raw.strip() or "0.0.0")


class _Resolve:
    def __init__(self, request: ResolveRequest, catalog: CatalogAccess) -> None:
        self.request = request
        self.catalog = catalog
        environment = request.environment
        self.env = environment if isinstance(environment, NuGetEnvironment) else NuGetEnvironment()
        self.deadline = time.monotonic() + request.timeout_s
        self.graph = ResolveGraph(strategy=STRATEGY, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        self.versions: dict[str, list[CatalogVersion]] = {}
        self.fetches = 0
        self.decided: dict[str, tuple[CatalogVersion, int]] = {}   # id -> (version, level)
        self.refs: dict[str, PackageRef] = {}
        self.errors: dict[str, list[str]] = defaultdict(list)

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s")

    async def candidates(self, ref: PackageRef) -> list[CatalogVersion]:
        if ref.name not in self.versions:
            self._check()
            versions = list(await self.catalog.versions(ref, self.request.profile))
            if not versions:
                if self.fetches >= self.request.max_lazy_fetches:
                    raise ResolveLimitExceeded("lazy_fetches", self.request.max_lazy_fetches)
                self.fetches += 1
                if await self.catalog.ensure(ref, self.request.profile):
                    self.graph.fetched.append(ref)
                    versions = list(await self.catalog.versions(ref, self.request.profile))
            self.versions[ref.name] = [v for v in versions if v.status in ("active", "unlisted") and _valid(v.version)]
        return self.versions[ref.name]

    async def choose(self, ref: PackageRef, ranges: list[NuGetConstraint]) -> CatalogVersion | None:
        """The lowest version every range allows (FindBestMatch for a single range), listed versions first."""
        versions = await self.candidates(ref)
        for pool in ([v for v in versions if v.status == "active"], versions):
            names = [v.version for v in pool]
            if len(ranges) == 1:
                best = ranges[0].best_match(names)
            else:
                allowed = [n for n in names if all(r.contains(n) for r in ranges)]
                best = min(allowed, key=_SCHEME.sort_key) if allowed else None
            if best is not None:
                return next(v for v in pool if v.version == best)
        return None

    async def walk(self) -> list[tuple[str, str, str, int]]:
        """Decide every package level by level; returns the edges (from id or "", to id, range, level)."""
        edges: list[tuple[str, str, str, int]] = []
        level_wants: list[tuple[str, PackageRef, str]] = [("", root.ref, root.constraint) for root in self.request.roots]
        level = 1
        while level_wants:
            self._check()
            by_id: dict[str, list[tuple[str, PackageRef, str]]] = defaultdict(list)
            for want in level_wants:
                by_id[want[1].name].append(want)
            next_wants: list[tuple[str, PackageRef, str]] = []
            for package_id, wants in sorted(by_id.items()):
                ranges = []
                for parent, _, raw in wants:
                    try:
                        ranges.append(_range(raw))
                    except InvalidVersion:
                        self.errors[parent].append(f"{package_id} {raw!r} is not a NuGet version range")
                if package_id in self.decided:
                    decided, _ = self.decided[package_id]
                    for (parent, _, raw), constraint in zip(wants, ranges):
                        edges.append((parent, package_id, raw, level))
                        if constraint.min is not None and _SCHEME.sort_key(decided.version) < _SCHEME.sort_key(constraint.min):
                            self.graph.warnings.append({
                                "code": "NU1605", "package": package_id,
                                "message": f"detected package downgrade: {package_id} from {constraint.min} to "
                                           f"{decided.version} (a nearer reference wins)"})
                    continue
                if not ranges:
                    continue
                chosen = await self.choose(wants[0][1], ranges)
                if chosen is None:
                    if len(wants) > 1:
                        raise _Aborted("NU1107", f"version conflict detected for {package_id}: "
                                                 f"{', '.join(sorted({w[2] for w in wants}))}", package=package_id)
                    self.errors[wants[0][0]].append(f"no version of {package_id} matches {wants[0][2]}")
                    continue
                if len(self.decided) >= self.request.max_nodes:
                    raise ResolveLimitExceeded("nodes", self.request.max_nodes)
                self.decided[package_id] = (chosen, level)
                self.refs[package_id] = wants[0][1]
                edges.extend((parent, package_id, raw, level) for parent, _, raw in wants)
                deps = await self.catalog.dependencies(wants[0][1], chosen.version, chosen.registry_id)
                if deps is None:
                    self.errors[package_id].append(UNKNOWN_DEPENDENCIES)
                    continue
                next_wants.extend((package_id, dep.target, dep.constraint) for dep in deps if self._applies(dep))
            level_wants = next_wants
            level += 1
        return edges

    def _applies(self, dep: NormalizedDependency) -> bool:
        return dependency_applies(dep, self.env, include_optional=False)

    async def build(self, edges: list[tuple[str, str, str, int]]) -> None:
        ids = {package_id: f"n{number}" for number, package_id in
               enumerate(sorted(self.decided, key=lambda p: (self.decided[p][1], p)), start=1)}
        roots = {root.ref.name for root in self.request.roots}
        for package_id, (version, level) in sorted(self.decided.items(), key=lambda item: ids[item[0]]):
            latest = None
            if package_id in roots:
                stable = [v.version for v in self.versions.get(package_id, [])
                          if v.status == "active" and not _SCHEME.is_prerelease(v.version)]
                latest = max(stable, key=_SCHEME.sort_key) if stable else None
            self.graph.nodes.append(ResolvedNode(
                id=ids[package_id], ref=self.refs[package_id], version=version.version,
                registry_slug=version.registry_slug, depth=level, errors=list(self.errors.get(package_id, [])),
                latest_compatible=latest))
        for parent, child, raw, _ in edges:
            if child in ids and (parent == "" or parent in ids):
                self.graph.edges.append(ResolvedEdge(source=ids[parent] if parent else "root", target=ids[child],
                                                     constraint=raw, dependency_type="runtime"))
        for message in self.errors.get("", []):
            self.graph.warnings.append({"code": "unresolvable", "message": message})


def _valid(version: str) -> bool:
    try:
        _SCHEME.validate(version)
    except InvalidVersion:
        return False
    return True


class NuGetResolver:
    emulates = ".NET SDK 8 (NuGet 6.x)"
    strategy = STRATEGY

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            edges = await state.walk()
            missing = [r.ref.name for r in request.roots if r.ref.name not in state.decided
                       and not state.versions.get(r.ref.name)]
            if missing:
                state.graph.error = {"code": "not_found", "packages": missing,
                                     "message": f"no source of the profile has {', '.join(missing)}"}
                return state.graph
            await state.build(edges)
        except ResolveLimitExceeded as exc:
            state.graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit,
                                 "value": exc.value, "packages_seen": len(state.versions), "lazy_fetches": state.fetches}
        except _Aborted as exc:
            state.graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(state.versions),
                                 **exc.details}
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            state.graph.error = {"code": "upstream_error", "message": f"{type(exc).__name__}: {exc}",
                                 "packages_seen": len(state.versions)}
        return state.graph


RESOLVER = NuGetResolver()
