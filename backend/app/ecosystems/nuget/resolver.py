"""NuGet resolve: the packages `dotnet restore` puts in project.assets.json (SPEC-MULTI-ECOSYSTEM.md §9.6).

The way NuGet's RemoteDependencyWalker and its conflict resolution do it:

1. **Walk.** Every edge takes its own best match (`VersionRange.FindBestMatch`: the lowest applicable version, for a
   floating range the highest matching the float; unlisted versions count, restore reads the full version list), and
   the walk continues below it with the dependencies of the version's nearest framework group. A package that loops
   back to an ancestor is a cycle (NU1108).
2. **Direct dependency wins.** An edge is eclipsed when an ancestor on its own path declares the same package directly
   (the project's references are ancestors of everything): it is not walked; when it wanted more than the nearer
   declaration gives, that is a downgrade (NU1605), which the .NET SDK treats as an error.
3. **Cousins.** References from different paths, at any depth, are cousins: the highest of their versions wins, the
   others drop out with everything only they brought, and that repeats until nothing changes. A reference whose range
   the winner does not satisfy fails the restore (NU1107).

A failed restore keeps its graph, as NuGet still writes the assets file, with the error code in `error`.

Warnings as restore gives them: every walked edge, whatever later wins, whose range has no inclusive lower bound
(NU1604 on the project's references, NU1602 below) or whose lower bound the feed lacks (NU1603: the best match is
higher); and an eclipsed edge whose range the nearer declaration's version exceeds (NU1608). Floating ranges give none.
"""

import time
from collections import defaultdict
from collections.abc import Sequence
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
)
from app.ecosystems.nuget.environment import NuGetEnvironment, dependency_applies, groups_of
from app.ecosystems.nuget.frameworks import nearest
from app.ecosystems.nuget.versions import NuGetConstraint, NuGetVersionScheme, parse_range

STRATEGY = "nuget-nearest-wins"
UNKNOWN_DEPENDENCIES = "dependencies unknown"
MAX_WALK = 50_000          # tree nodes; the graph is far smaller, the tree repeats shared subtrees per path
_SCHEME = NuGetVersionScheme()


@dataclass(eq=False)
class _Node:
    package_id: str
    ref: PackageRef | None              # None: the project
    raw: str                            # the range as written on the edge
    constraint: NuGetConstraint | None
    version: CatalogVersion | None
    parent: "_Node | None"
    depth: int
    children: list["_Node"] = field(default_factory=list)
    declared: dict[str, "_Node | None"] = field(default_factory=dict)   # direct dependencies by id -> their node


class _Aborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _range(raw: str) -> NuGetConstraint:
    # A dependency without a range takes any version, the lowest first (`[0.0.0, )`).
    return parse_range(raw.strip() or "0.0.0")


def _pretty(raw: str, constraint: NuGetConstraint) -> str:
    """A range as restore prints it in messages: ` (>= 1.0.0 && < 2.0.0)`, ` (= 1.0.0)`; nothing for no range."""
    if not raw.strip():
        return ""
    if constraint.min is not None and constraint.min == constraint.max:
        return f" (= {constraint.min})"
    parts = []
    if constraint.min is not None:
        parts.append(f"{'>=' if constraint.min_inclusive else '>'} {constraint.min}")
    if constraint.max is not None:
        parts.append(f"{'<=' if constraint.max_inclusive else '<'} {constraint.max}")
    return f" ({' && '.join(parts)})"


def _valid(version: str) -> bool:
    try:
        _SCHEME.validate(version)
    except InvalidVersion:
        return False
    return True


class _Resolve:
    def __init__(self, request: ResolveRequest, catalog: CatalogAccess) -> None:
        self.request = request
        self.catalog = catalog
        environment = request.environment
        self.env = environment if isinstance(environment, NuGetEnvironment) else NuGetEnvironment()
        self.deadline = time.monotonic() + request.timeout_s
        self.graph = ResolveGraph(strategy=STRATEGY, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        self.versions: dict[str, list[CatalogVersion]] = {}
        self.deps: dict[tuple[str, str], list[NormalizedDependency] | None] = {}
        self.fetches = 0
        self.walked = 0
        self.errors: dict[tuple[str, str], list[str]] = defaultdict(list)   # (id, version) -> node errors
        self.downgrades: list[tuple[_Node, str, str]] = []   # (eclipsed edge's parent, id, wanted range)
        self.cycles: list[str] = []
        self.root = _Node("", None, "", None, None, None, 0)
        self.warnings: dict[str, dict[str, str]] = {}          # message -> warning, in the order restore finds them

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

    async def dependencies(self, ref: PackageRef, version: CatalogVersion) -> list[NormalizedDependency] | None:
        key = (ref.name, version.version)
        if key not in self.deps:
            self._check()
            found = await self.catalog.dependencies(ref, version.version, version.registry_id)
            self.deps[key] = None if found is None else [
                dep for dep in found if dependency_applies(dep, self.env, include_optional=False)]
            if found and not self.deps[key] and nearest(self.env.framework, groups_of(found[0])) is None:
                # NuGet would fall back to a .NET Framework group (AssetTargetFallback), which Q7 leaves out.
                self.graph.warnings.append({
                    "code": "no_framework_group", "package": ref.name,
                    "message": f"{ref.name} {version.version} has no dependency group for {self.env.target_framework}; "
                               f"its .NET Framework or platform groups are not used"})
        return self.deps[key]

    # --- 1-2: the walk ---------------------------------------------------------------------------------------------

    async def walk(self) -> None:
        wants = [(root.ref, root.constraint) for root in self.request.roots]
        await self._expand(self.root, wants)

    async def _expand(self, node: _Node, wants: Sequence[tuple[PackageRef, str]]) -> None:
        node.declared = {ref.name: None for ref, _ in wants}
        for ref, raw in wants:
            self.walked += 1
            if self.walked > MAX_WALK:
                raise ResolveLimitExceeded("nodes", self.request.max_nodes)
            if self._is_ancestor(node, ref.name):
                self.cycles.append(f"{self._path(node)} -> {ref.name}")
                continue
            eclipsing = self._eclipsing(node, ref.name)
            if eclipsing is not None:
                self.downgrades.append((node, ref.name, raw))
                continue
            try:
                constraint = _range(raw)
            except InvalidVersion:
                self._node_error(node, f"{ref.name} {raw!r} is not a NuGet version range")
                continue
            names = [v.version for v in await self.candidates(ref)]
            best = constraint.best_match(names)
            version = next((v for v in self.versions[ref.name] if v.version == best), None) if best else None
            child = _Node(ref.name, ref, raw, constraint, version, node, node.depth + 1)
            node.children.append(child)
            node.declared[ref.name] = child
            if version is None:
                self._node_error(node, f"no version of {ref.name} matches {raw}")
                continue
            self._lower_bound(node, child)
            deps = await self.dependencies(ref, version)
            if deps is None:
                self.errors[(ref.name, version.version)].append(UNKNOWN_DEPENDENCIES)
                continue
            await self._expand(child, [(dep.target, dep.constraint) for dep in deps])

    def _warn(self, code: str, package_id: str, message: str) -> None:
        self.warnings.setdefault(message, {"code": code, "package": package_id, "message": message})

    def _who(self, node: _Node) -> str:
        return f"{node.package_id} {node.version.version}" if node.version is not None else "the project"

    def _lower_bound(self, parent: _Node, child: _Node) -> None:
        """NU1604, NU1602, NU1603 for one walked edge."""
        constraint, version = child.constraint, child.version
        if constraint is None or version is None or constraint.floating is not None:
            return
        pretty = _pretty(child.raw, constraint)
        if not child.raw.strip() or constraint.min is None or not constraint.min_inclusive:
            if parent is self.root:
                self._warn("NU1604", child.package_id,
                           f"Project dependency {child.package_id}{pretty} does not contain an inclusive lower bound. "
                           f"Include a lower bound in the dependency version to ensure consistent restore results.")
            else:
                self._warn("NU1602", child.package_id,
                           f"{self._who(parent)} does not provide an inclusive lower bound for dependency "
                           f"{child.package_id}{pretty}. An approximate best match of {child.package_id} "
                           f"{version.version} was resolved.")
        elif _SCHEME.sort_key(version.version) != _SCHEME.sort_key(constraint.min):
            self._warn("NU1603", child.package_id,
                       f"{self._who(parent)} depends on {child.package_id}{pretty} but {child.package_id} "
                       f"{constraint.min} was not found. An approximate best match of {child.package_id} "
                       f"{version.version} was resolved.")

    @staticmethod
    def _is_ancestor(node: _Node, package_id: str) -> bool:
        current: _Node | None = node
        while current is not None:
            if current.package_id == package_id:
                return True
            current = current.parent
        return False

    @staticmethod
    def _eclipsing(node: _Node, package_id: str) -> _Node | None:
        """The ancestor (strictly above `node`) that declares `package_id` directly, whose declaration wins."""
        current = node.parent
        while current is not None:
            if package_id in current.declared:
                return current
            current = current.parent
        return None

    @staticmethod
    def _path(node: _Node) -> str:
        names: list[str] = []
        current: _Node | None = node
        while current is not None and current.ref is not None:
            names.append(current.package_id)
            current = current.parent
        return " -> ".join(reversed(names))

    def _node_error(self, node: _Node, message: str) -> None:
        key = (node.package_id, node.version.version) if node.version is not None else ("", "")
        if message not in self.errors[key]:
            self.errors[key].append(message)

    # --- 3: cousins ------------------------------------------------------------------------------------------------

    def settle(self) -> tuple[dict[str, CatalogVersion], set[int]]:
        """Pick the highest version per package among the references still in the graph, drop the losers' subtrees,
        repeat until stable; returns the winners and the ids (`id()`) of the nodes still in the graph."""
        rejected: set[int] = set()
        while True:
            live = self._live(rejected)
            winners: dict[str, CatalogVersion] = {}
            for node in live:
                if node.version is not None:
                    best = winners.get(node.package_id)
                    if best is None or _SCHEME.sort_key(node.version.version) > _SCHEME.sort_key(best.version):
                        winners[node.package_id] = node.version
            losers = {id(node) for node in live if node.version is not None
                      and node.version.version != winners[node.package_id].version}
            if not losers:
                return winners, {id(node) for node in live}
            rejected |= losers

    def _live(self, rejected: set[int]) -> list[_Node]:
        live: list[_Node] = []
        stack = list(self.root.children)
        while stack:
            node = stack.pop()
            if id(node) in rejected:
                continue
            live.append(node)
            stack.extend(node.children)
        return live

    def warn_eclipsed(self, winners: dict[str, CatalogVersion], live: set[int]) -> None:
        """NU1608 for an eclipsed edge (under a node still in the graph) whose range the winner exceeds."""
        for parent, package_id, raw in self.downgrades:
            winner = winners.get(package_id)
            if winner is None or parent is self.root or id(parent) not in live:
                continue
            try:
                constraint = _range(raw)
            except InvalidVersion:
                continue
            below = constraint.min is not None and _SCHEME.sort_key(winner.version) < _SCHEME.sort_key(constraint.min)
            if not constraint.contains(winner.version) and not below:      # below the range is NU1605
                self._warn("NU1608", package_id,
                           f"Detected package version outside of dependency constraint: {self._who(parent)} requires "
                           f"{package_id}{_pretty(raw, constraint)} but version {package_id} {winner.version} "
                           f"was resolved.")
        self.graph.warnings.extend(self.warnings.values())

    def check(self, winners: dict[str, CatalogVersion], live: set[int]) -> None:
        """NU1107 for a reference (from a node still in the graph) the winner does not satisfy; NU1605 for a downgrade
        under a node still in the graph; NU1108 for a cycle."""
        all_nodes = self._live(set())
        for node in all_nodes:
            parent_live = node.parent is self.root or (node.parent is not None and id(node.parent) in live)
            winner = winners.get(node.package_id)
            if parent_live and winner is not None and node.constraint is not None \
                    and not node.constraint.contains(winner.version):
                raise _Aborted("NU1107", f"version conflict detected for {node.package_id}: {node.raw} "
                                         f"(from {self._path(node.parent or self.root) or 'the project'}) and "
                                         f"{winner.version}")
        for parent, package_id, raw in self.downgrades:
            if parent is not self.root and id(parent) not in live:
                continue
            winner = winners.get(package_id)
            try:
                constraint = _range(raw)
            except InvalidVersion:
                continue
            if winner is not None and constraint.min is not None \
                    and _SCHEME.sort_key(winner.version) < _SCHEME.sort_key(constraint.min):
                raise _Aborted("NU1605", f"detected package downgrade: {package_id} from {constraint.min} to "
                                         f"{winner.version} (reference from {self._path(parent) or 'the project'})")
        if self.cycles:
            raise _Aborted("NU1108", f"cycle detected: {self.cycles[0]}")

    def build(self, winners: dict[str, CatalogVersion], live: set[int]) -> None:
        nodes = [n for n in self._live(set()) if id(n) in live and n.version is not None]
        depth: dict[str, int] = {}
        refs: dict[str, PackageRef] = {}
        for node in nodes:
            depth[node.package_id] = min(depth.get(node.package_id, 10**9), node.depth)
            refs[node.package_id] = node.ref or PackageRef("nuget", "", node.package_id)
        ids = {pid: f"n{n}" for n, pid in enumerate(sorted(winners, key=lambda p: (depth.get(p, 0), p)), start=1)}
        roots = {root.ref.name for root in self.request.roots}
        for package_id, version in sorted(winners.items(), key=lambda item: ids[item[0]]):
            latest = None
            if package_id in roots:
                stable = [v.version for v in self.versions.get(package_id, [])
                          if v.status == "active" and not _SCHEME.is_prerelease(v.version)]
                latest = max(stable, key=_SCHEME.sort_key) if stable else None
            self.graph.nodes.append(ResolvedNode(
                id=ids[package_id], ref=refs[package_id], version=version.version, registry_slug=version.registry_slug,
                depth=depth.get(package_id, 1), errors=list(self.errors.get((package_id, version.version), [])),
                latest_compatible=latest))
        seen: set[tuple[str, str]] = set()
        for node in nodes:
            source = "root" if node.parent is self.root else ids.get(node.parent.package_id, "") if node.parent else ""
            if source and (source, ids[node.package_id]) not in seen:
                seen.add((source, ids[node.package_id]))
                self.graph.edges.append(ResolvedEdge(source=source, target=ids[node.package_id], constraint=node.raw,
                                                     dependency_type="runtime"))
        for message in self.errors.get(("", ""), []):
            self.graph.warnings.append({"code": "unresolvable", "message": message})


class NuGetResolver:
    emulates = ".NET SDK 8 (NuGet 6.x)"
    strategy = STRATEGY

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            await state.walk()
            missing = [r.ref.name for r in request.roots if not state.versions.get(r.ref.name)]
            if missing:
                state.graph.error = {"code": "not_found", "packages": missing,
                                     "message": f"no source of the profile has {', '.join(missing)}"}
                return state.graph
            winners, live = state.settle()
            state.build(winners, live)
            state.warn_eclipsed(winners, live)
            state.check(winners, live)
        except ResolveLimitExceeded as exc:
            state.graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit,
                                 "value": exc.value, "packages_seen": len(state.versions), "lazy_fetches": state.fetches}
        except _Aborted as exc:
            # The graph stays: NuGet writes the assets file of a failed restore too.
            state.graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(state.versions)}
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            state.graph.error = {"code": "upstream_error", "message": f"{type(exc).__name__}: {exc}",
                                 "packages_seen": len(state.versions)}
        return state.graph


RESOLVER = NuGetResolver()
