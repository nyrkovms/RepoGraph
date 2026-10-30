"""npm resolve: the tree npm 10 builds (arborist), as a graph of (package, version) nodes (SPEC-MULTI-ECOSYSTEM.md §9.3).

Breadth-first from the roots, dependencies of a node in name order. A dependency is looked for up the chain of
`node_modules` folders from where it is needed: a version found there that satisfies it is reused (deduplication),
one that does not stops the search and the new version goes just below it; otherwise it goes as high as nothing
conflicts (npm's hoisting). The version is the one npm-pick-manifest picks: `dist-tags.latest` if it satisfies,
otherwise the best of the matching versions by: not deprecated, compatible with the Node.js version, highest.
peerDependencies (npm >= 7 installs them) go next to the package that asks for them; an unsatisfiable one is a peer
conflict. Optional dependencies whose version does not fit the os/cpu are skipped; aliases (`npm:`) are followed;
git/file/URL specs cannot be resolved from a registry and are reported on the node that declares them.
"""

import time
from collections import deque
from collections.abc import Mapping, Sequence
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
from app.ecosystems.npm.environment import NpmEnvironment, version_allows
from app.ecosystems.npm.versions import NpmVersionScheme, parse

STRATEGY = "npm-arborist"
UNKNOWN_DEPENDENCIES = "dependencies unknown"
NOT_FOUND_PREFIX = "no source has "
_SCHEME = NpmVersionScheme()


@dataclass(eq=False)
class _Node:
    """A folder in the tree: `name` is the folder (the alias for `npm:` dependencies), `ref` the package in it."""

    name: str
    ref: PackageRef | None              # None: the virtual root
    version: CatalogVersion | None
    parent: "_Node | None"
    depth: int                          # shortest distance from the roots in the graph, not the folder depth
    id: str = ""
    children: dict[str, "_Node"] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Want:
    name: str                           # folder name
    ref: PackageRef
    constraint: str
    dependency_type: str
    details: Mapping[str, object]


class _Aborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _display(ref: PackageRef) -> str:
    return f"@{ref.namespace}/{ref.name}" if ref.namespace else ref.name


def _satisfies(version: str, constraint: str) -> bool:
    """semver.satisfies: `*` and an empty range match no prerelease; a dist-tag is not a range."""
    try:
        return _SCHEME.constraint(constraint).contains(version)
    except InvalidVersion:
        return False


def _reuses(version: str, constraint: str) -> bool:
    """arborist's dep-valid: an installed version serves `*`, an empty range and a dist-tag whatever it is."""
    spec = constraint.strip()
    if spec in ("", "*"):
        return True
    try:
        return _SCHEME.constraint(spec).contains(version)
    except InvalidVersion:
        return True


class _Resolve:
    def __init__(self, request: ResolveRequest, catalog: CatalogAccess) -> None:
        self.request = request
        self.catalog = catalog
        environment = request.environment
        self.env = environment if isinstance(environment, NpmEnvironment) else NpmEnvironment()
        self.deadline = time.monotonic() + request.timeout_s
        self.versions: dict[PackageRef, list[CatalogVersion]] = {}
        self.fetches = 0
        self.graph = ResolveGraph(strategy=STRATEGY, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        self.root = _Node("", None, None, None, 0)
        self.count = 0

    # --- the catalog ---------------------------------------------------------------------------------------------

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s")

    async def candidates(self, ref: PackageRef) -> list[CatalogVersion]:
        if ref not in self.versions:
            self._check()
            versions = list(await self.catalog.versions(ref, self.request.profile))
            if not versions:
                if self.fetches >= self.request.max_lazy_fetches:
                    raise ResolveLimitExceeded("lazy_fetches", self.request.max_lazy_fetches)
                self.fetches += 1
                if await self.catalog.ensure(ref, self.request.profile):
                    self.graph.fetched.append(ref)
                    versions = list(await self.catalog.versions(ref, self.request.profile))
            self.versions[ref] = [v for v in versions if v.status == "active" and _valid(v.version)]
        return self.versions[ref]

    def pick(self, versions: Sequence[CatalogVersion], constraint: str) -> CatalogVersion | None:
        """npm-pick-manifest: a dist-tag names its version; `latest` wins if it satisfies (`*` takes it even as a
        prerelease) and is usable; otherwise the matching versions, prereleases only when the range names one,
        ordered by not deprecated, engine-compatible, highest."""
        spec = constraint.strip()
        tagged = {tag: v for v in versions for tag in v.tags}
        if spec in tagged:
            return tagged[spec]
        latest = tagged.get("latest")
        if latest is not None and (spec in ("", "*") or _satisfies(latest.version, spec)) and not latest.deprecated \
                and self._engine_ok(latest):
            return latest
        matching = [v for v in versions if _satisfies(v.version, spec)]
        if not matching:
            return None
        return max(matching, key=lambda v: (not v.deprecated, self._engine_ok(v), _SCHEME.sort_key(v.version)))

    def _engine_ok(self, version: CatalogVersion) -> bool:
        node = (version.engines or {}).get("node")
        return node is None or _satisfies(self.env.node_version, node)

    # --- the tree ------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        queue: deque[_Node] = deque()
        for root in self.request.roots:
            want = _Want(_display(root.ref), root.ref, root.constraint, "runtime", {})
            await self.place(self.root, want, queue, from_root=True)
        while queue:
            node = queue.popleft()
            await self.expand(node, queue)

    async def expand(self, node: _Node, queue: deque[_Node]) -> None:
        assert node.ref is not None and node.version is not None
        deps = await self.catalog.dependencies(node.ref, node.version.version, node.version.registry_id)
        if deps is None:
            node.errors.append(UNKNOWN_DEPENDENCIES)
            self.graph.warnings.append({"code": "dependencies_unknown", "node": node.id,
                                        "message": f"dependencies of {_display(node.ref)} {node.version.version} "
                                                   "are unknown"})
            return
        wants: list[_Want] = []
        for dep in deps:
            want = self._want(node, dep)
            if want is not None:
                wants.append(want)
        for want in sorted(wants, key=lambda w: w.name):
            await self.place(node, want, queue)

    def _want(self, node: _Node, dep: NormalizedDependency) -> _Want | None:
        details = dict(dep.details or {})
        name = dep.alias or _display(dep.target)
        if dep.dependency_type in ("dev", "build", "provided"):
            return None
        if dep.dependency_type == "peer" and details.get("optional"):
            return None                         # npm does not install optional peers
        if details.get("unresolvable"):
            node.errors.append(f"unresolvable dependency {name}: {details['unresolvable']}")
            self.graph.warnings.append({"code": "dependency_unresolvable", "node": node.id, "package": name,
                                        "spec": details["unresolvable"]})
            return None
        if dep.alias:
            details["alias"] = dep.alias
        return _Want(name, dep.target, dep.constraint, dep.dependency_type, details)

    async def place(self, dependent: _Node, want: _Want, queue: deque[_Node], from_root: bool = False) -> None:
        # Peers must be visible from the dependent's folder, so they are placed next to it.
        start = dependent if from_root or want.dependency_type != "peer" else (dependent.parent or self.root)
        target: _Node | None = None
        level: _Node | None = start
        while level is not None:
            existing = level.children.get(want.name)
            if existing is not None:
                if existing.ref == want.ref and existing.version is not None \
                        and _reuses(existing.version.version, want.constraint):
                    self._edge(dependent, existing, want)
                    return
                break                       # another version here: the new one goes just below it
            target = level
            level = level.parent
        if target is None:
            # The folder the dependency must be visible from already holds another version of it.
            existing = start.children[want.name]
            if want.dependency_type == "peer":
                self._peer_conflict(dependent, want, existing)
            else:
                self.graph.conflicts.append({"kind": "unsatisfiable", "package": _display(want.ref),
                                             "constraint": want.constraint, "required_by": dependent.id or "root"})
                dependent.errors.append(f"{want.name} is already {existing.version.version if existing.version else '?'}")
            return
        versions = await self.candidates(want.ref)
        if not versions:
            dependent.errors.append(f"{NOT_FOUND_PREFIX}{_display(want.ref)}")
            self.graph.warnings.append({"code": "dependency_not_found", "node": dependent.id,
                                        "package": _display(want.ref)})
            return
        chosen = self.pick(versions, want.constraint)
        if chosen is None:
            if want.dependency_type == "optional":
                return
            self.graph.conflicts.append({"kind": "unsatisfiable", "package": _display(want.ref),
                                         "constraint": want.constraint, "required_by": dependent.id or "root"})
            dependent.errors.append(f"no version of {_display(want.ref)} matches {want.constraint}")
            return
        if not version_allows(chosen.engines, self.env):
            if want.dependency_type == "optional":
                return                      # skipped on this platform, as npm does
            dependent.errors.append(f"{_display(want.ref)} {chosen.version} does not support "
                                    f"{self.env.os}/{self.env.cpu}")
        if self.count >= self.request.max_nodes:
            raise ResolveLimitExceeded("nodes", self.request.max_nodes)
        self.count += 1
        node = _Node(want.name, want.ref, chosen, target, dependent.depth + 1, id=f"n{self.count}")
        target.children[want.name] = node
        self._edge(dependent, node, want)
        if not self._engine_ok(chosen):
            # npm installs it anyway and warns (EBADENGINE).
            self.graph.warnings.append({"code": "engine_node", "node": node.id, "package": _display(want.ref),
                                        "version": chosen.version, "engines": (chosen.engines or {}).get("node"),
                                        "node_version": self.env.node_version})
        queue.append(node)

    def _edge(self, dependent: _Node, node: _Node, want: _Want) -> None:
        self.graph.edges.append(ResolvedEdge(source=dependent.id or "root", target=node.id, constraint=want.constraint,
                                             dependency_type=want.dependency_type,  # type: ignore[arg-type]
                                             details=dict(want.details)))

    def _peer_conflict(self, dependent: _Node, want: _Want, existing: _Node) -> None:
        version = existing.version.version if existing.version else None
        self.graph.conflicts.append({"kind": "peer", "package": _display(want.ref), "constraint": want.constraint,
                                     "required_by": dependent.id, "installed": version})
        dependent.errors.append(f"peer {_display(want.ref)}@{want.constraint} conflicts with {version}")

    # --- the answer ------------------------------------------------------------------------------------------------

    async def nodes(self) -> list[ResolvedNode]:
        out: list[ResolvedNode] = []
        roots = {want.target for want in self.graph.edges if want.source == "root"}
        stack = sorted(self.root.children.values(), key=lambda n: n.id)
        all_nodes: list[_Node] = []
        while stack:
            node = stack.pop(0)
            all_nodes.append(node)
            stack.extend(sorted(node.children.values(), key=lambda n: n.id))
        for node in sorted(all_nodes, key=lambda n: int(n.id[1:])):
            assert node.ref is not None and node.version is not None
            latest = None
            if node.id in roots:
                best = self.pick(await self.candidates(node.ref), "*")
                latest = best.version if best else None
            out.append(ResolvedNode(id=node.id, ref=node.ref, version=node.version.version,
                                    registry_slug=node.version.registry_slug, depth=node.depth,
                                    errors=list(node.errors), latest_compatible=latest,
                                    details={"bundled": True} if node.version.bundled else {}))
        return out


def _valid(version: str) -> bool:
    try:
        parse(version)
    except InvalidVersion:
        return False
    return True


class NpmResolver:
    emulates = "npm 10 (arborist)"
    strategy = STRATEGY

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            await state.run()
        except ResolveLimitExceeded as exc:
            state.graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit,
                                 "value": exc.value, "packages_seen": len(state.versions),
                                 "lazy_fetches": state.fetches}
            return state.graph
        except _Aborted as exc:
            state.graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(state.versions)}
            return state.graph
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            state.graph.error = {"code": "upstream_error", "message": f"{type(exc).__name__}: {exc}",
                                 "packages_seen": len(state.versions)}
            return state.graph
        missing_roots = [_display(r.ref) for r in request.roots if not state.versions.get(r.ref)]
        if missing_roots:
            state.graph.error = {"code": "not_found", "message": f"no source of the profile has {', '.join(missing_roots)}",
                                 "packages": missing_roots}
            return state.graph
        state.graph.nodes = await state.nodes()
        return state.graph


RESOLVER = NpmResolver()
