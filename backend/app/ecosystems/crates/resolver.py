"""crates resolve: the versions Cargo's resolver picks (`Cargo.lock`) and the graph for a target
(SPEC-MULTI-ECOSYSTEM.md §9.4).

Pass 1 chooses versions the way Cargo's dependency resolver does: one activation per (crate, semver-compatibility
class), so `rand 0.7` and `rand 0.8` can live side by side; the dependency with the fewest candidates goes next;
candidates newest first, yanked ones only for an `=` pin; a candidate whose class already holds another version, or
that lacks a feature asked of it, is skipped, and a dependency left without candidates backtracks to the last choice
that had alternatives. Features asked of an activation by all its dependents are unified, and optional dependencies
come in through them (`dep:x`, `x/feat`, a bare `x`; `x?/feat` does not turn `x` on). Like Cargo.lock it ignores the
platform: dependencies of every target take part.

Pass 2 walks the chosen versions from the roots again, following only the dependencies that apply on the target and
recomputing features along those edges, which is what `cargo tree --target <t>` shows. Target `*` keeps every platform.

The identity of a dependency (which class it lands in) is known only once a candidate is picked: `>=0.9` matches 0.9.x
and 1.x alike. That is why this is a search of its own, as Cargo's is, rather than resolvelib with fixed identities.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from app.ecosystems.base import (
    CatalogAccess,
    CatalogVersion,
    DependencyType,
    InvalidVersion,
    NormalizedDependency,
    PackageRef,
    ResolvedEdge,
    ResolvedNode,
    ResolveGraph,
    ResolveLimitExceeded,
    ResolveRequest,
)
from app.ecosystems.crates.cfg import platform_matches
from app.ecosystems.crates.environment import ALL_TARGETS, CratesEnvironment
from app.ecosystems.crates.versions import CargoConstraint, CratesVersionScheme, compatibility, parse

STRATEGY = "cargo-resolver"
UNKNOWN_DEPENDENCIES = "dependencies unknown"
MAX_STEPS = 50_000
_SCHEME = CratesVersionScheme()

Key = tuple[str, str]                   # (crate, compatibility class)
ROOT: Key = ("", "root")


@dataclass(frozen=True, slots=True)
class _Want:
    """One dependency entry of an activation (or a root), with the features asked of its target."""

    parent: Key
    index: int                          # position in the parent's dependency list: the edge's identity
    ref: PackageRef
    constraint: str
    features: frozenset[str]
    dependency_type: DependencyType
    rust_cfg: str | None = None


@dataclass(frozen=True, slots=True)
class _Activation:
    version: CatalogVersion
    features: frozenset[str]


@dataclass(slots=True)
class _State:
    activations: dict[Key, _Activation] = field(default_factory=dict)
    pending: list[_Want] = field(default_factory=list)
    chosen: dict[tuple[Key, int], Key] = field(default_factory=dict)   # (parent, entry) -> activation

    def copy(self) -> "_State":
        return _State(dict(self.activations), list(self.pending), dict(self.chosen))


@dataclass(slots=True)
class _Frame:
    state: _State                       # before the choice
    want: _Want
    alternatives: list[CatalogVersion]


class _Aborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Enabled:
    entries: frozenset[int]             # dependency entries switched on
    asks: Mapping[str, frozenset[str]]  # local dependency name -> features asked of it (`x/feat`)
    missing: frozenset[str]             # asked features the version does not have


def _local(dep: NormalizedDependency) -> str:
    return dep.alias or dep.target.name


def enabled(features: Mapping[str, Sequence[str]], deps: Sequence[NormalizedDependency], asked: frozenset[str],
            include_dev: bool = False) -> _Enabled:
    """Which dependency entries `asked` features switch on, and the features they ask of each dependency."""
    by_local: dict[str, list[int]] = {}
    for index, dep in enumerate(deps):
        by_local.setdefault(_local(dep), []).append(index)
    optional = {_local(dep) for dep in deps if dep.dependency_type == "optional"}
    explicit_dep = {value[4:] for values in features.values() for value in values if value.startswith("dep:")}
    implicit = {name for name in optional if name not in explicit_dep and name not in features}

    on: set[str] = set()
    asks: dict[str, set[str]] = {}
    weak: list[tuple[str, str]] = []
    missing: set[str] = set()
    seen: set[str] = set()
    todo = list(asked)
    while todo:
        value = todo.pop()
        if value in seen:
            continue
        seen.add(value)
        if value.startswith("dep:"):
            on.add(value[4:])
        elif "/" in value:
            name, _, feature = value.partition("/")
            if name.endswith("?"):
                weak.append((name[:-1], feature))
            else:
                if name in optional:
                    on.add(name)
                    if name in features and name not in explicit_dep:
                        todo.append(name)      # old syntax: the feature named after the dependency comes along
                asks.setdefault(name, set()).add(feature)
        elif value in features:
            todo.extend(features[value])
        elif value in implicit:
            on.add(value)
        elif value != "default":
            missing.add(value)
    for name, feature in weak:
        if name in on or name not in optional:
            asks.setdefault(name, set()).add(feature)

    entries: set[int] = set()
    for index, dep in enumerate(deps):
        kind = dep.dependency_type
        if kind == "dev" and not include_dev:
            continue
        if kind == "optional" and _local(dep) not in on:
            continue
        entries.add(index)
    return _Enabled(frozenset(entries), {name: frozenset(f) for name, f in asks.items()}, frozenset(missing))


def _asked_of(dep: NormalizedDependency, asks: Mapping[str, frozenset[str]]) -> frozenset[str]:
    details = dep.details or {}
    listed = details.get("features")
    features = {str(f) for f in listed} if isinstance(listed, list) else set()
    if details.get("default_features", True) is not False:
        features.add("default")
    return frozenset(features | asks.get(_local(dep), frozenset()))


def _valid(version: str) -> bool:
    try:
        parse(version)
    except InvalidVersion:
        return False
    return True


class _Resolve:
    def __init__(self, request: ResolveRequest, catalog: CatalogAccess) -> None:
        self.request = request
        self.catalog = catalog
        environment = request.environment
        self.env = environment if isinstance(environment, CratesEnvironment) else CratesEnvironment()
        self.deadline = time.monotonic() + request.timeout_s
        self.versions: dict[PackageRef, list[CatalogVersion]] = {}
        self.deps: dict[tuple[PackageRef, str], list[NormalizedDependency] | None] = {}
        self.fetches = 0
        self.steps = 0
        self.graph = ResolveGraph(strategy=STRATEGY, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        self.refs: dict[Key, PackageRef] = {}
        self.errors: dict[Key, list[str]] = {}
        self.root_wants: list[_Want] = []
        self._matching: dict[tuple[PackageRef, str], list[CatalogVersion]] = {}

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s")

    # --- the catalog ---------------------------------------------------------------------------------------------

    async def candidates(self, ref: PackageRef) -> list[CatalogVersion]:
        """Every version of the crate the profile shows, newest first."""
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
            usable = [v for v in versions if v.status in ("active", "yanked") and _valid(v.version)]
            self.versions[ref] = sorted(usable, key=lambda v: _SCHEME.sort_key(v.version), reverse=True)
        return self.versions[ref]

    async def dependencies(self, ref: PackageRef, version: CatalogVersion) -> list[NormalizedDependency] | None:
        key = (ref, version.version)
        if key not in self.deps:
            self._check()
            found = await self.catalog.dependencies(ref, version.version, version.registry_id)
            self.deps[key] = list(found) if found is not None else None
        return self.deps[key]

    async def matching(self, want: _Want) -> list[CatalogVersion]:
        """Versions the requirement accepts, newest first; yanked ones only when it pins exactly that version."""
        key = (want.ref, want.constraint)
        if key not in self._matching:
            try:
                constraint = _SCHEME.constraint(want.constraint)
            except InvalidVersion:
                constraint = None
            pinned = constraint.pinned() if isinstance(constraint, CargoConstraint) else None
            self._matching[key] = [] if constraint is None else [
                v for v in await self.candidates(want.ref) if constraint.contains(v.version)
                and (v.status == "active" or v.version == pinned)]
        return self._matching[key]

    # --- pass 1: versions ----------------------------------------------------------------------------------------

    def _error(self, key: Key, message: str) -> None:
        if message not in self.errors.setdefault(key, []):
            self.errors[key].append(message)

    async def _activate(self, state: _State, want: _Want, version: CatalogVersion) -> bool:
        """Activate `version` for `want` (or add features to its activation); False if it cannot take them."""
        key = (want.ref.name, compatibility(version.version))
        current = state.activations.get(key)
        if current is not None and current.version.version != version.version:
            return False
        features = (current.features if current else frozenset()) | want.features
        deps = await self.dependencies(want.ref, version)
        switched = enabled(version.features or {}, deps or [], features,
                           include_dev=self.request.include_dev and want.parent == ROOT)
        if switched.missing:
            return False                       # Cargo: "package does not have that feature"
        if current is not None and current.features == features:
            state.chosen[(want.parent, want.index)] = key
            return True
        if len(state.activations) + (current is None) > self.request.max_nodes:
            raise ResolveLimitExceeded("nodes", self.request.max_nodes)
        state.activations[key] = _Activation(version, features)
        state.chosen[(want.parent, want.index)] = key
        self.refs[key] = want.ref
        if deps is None:
            self._error(key, UNKNOWN_DEPENDENCIES)
            return True
        # Its dependencies come (back) with the features now asked; ones already satisfied are reused at once.
        state.pending.extend(
            _Want(key, index, dep.target, dep.constraint, _asked_of(dep, switched.asks), dep.dependency_type,
                  _rust_cfg(dep))
            for index, dep in enumerate(deps) if index in switched.entries
        )
        return True

    async def choose(self) -> _State:
        state = _State()
        for index, root in enumerate(self.request.roots):
            features = frozenset(root.extras) | frozenset(self.env.features)
            want = _Want(ROOT, index, root.ref, root.constraint or "*",
                         features | ({"default"} if self.env.default_features else frozenset()), "runtime")
            self.root_wants.append(want)
            state.pending.append(want)
        frames: list[_Frame] = []
        while state.pending:
            self.steps += 1
            if self.steps > MAX_STEPS:
                raise _Aborted("resolve_too_complex", f"no solution found in {MAX_STEPS} steps")
            self._check()
            sizes = [len(await self.matching(want)) for want in state.pending]
            position = min(range(len(sizes)), key=lambda i: (sizes[i], i))
            want = state.pending.pop(position)
            options = await self.matching(want)
            if not options:
                # Nothing matches at all: no choice made earlier can help. A partial graph says why on the node.
                self._error(want.parent, f"no version of {want.ref.name} matches {want.constraint}")
                continue
            before = state.copy()
            # A refused candidate leaves the state untouched: _activate changes it only once it accepts.
            for tried, version in enumerate(options):
                if await self._activate(state, want, version):
                    if tried + 1 < len(options):
                        before.pending.insert(position, want)
                        frames.append(_Frame(before, want, options[tried + 1:]))
                    break
            else:
                state = await self._backtrack(frames, want)
        return state

    async def _backtrack(self, frames: list[_Frame], failed: _Want) -> _State:
        while frames:
            frame = frames.pop()
            while frame.alternatives:
                version = frame.alternatives.pop(0)
                state = frame.state.copy()
                state.pending.remove(frame.want)
                if await self._activate(state, frame.want, version):
                    if frame.alternatives:
                        frames.append(frame)
                    return state
        raise _Aborted("resolution_impossible",
                       f"no versions satisfy every requirement on {failed.ref.name} ({failed.constraint})")

    # --- pass 2: the graph for the target ------------------------------------------------------------------------

    async def graph_for_target(self, state: _State) -> None:
        all_targets = self.env.target == ALL_TARGETS
        features: dict[Key, frozenset[str]] = {}
        edges: dict[tuple[Key, int], tuple[Key, ResolvedEdge]] = {}
        depth: dict[Key, int] = {}
        todo: list[Key] = []

        def reach(parent: Key, index: int, want_type: DependencyType, constraint: str, asked: frozenset[str],
                  rust_cfg: str | None) -> None:
            child = state.chosen.get((parent, index))
            if child is None:
                return
            depth[child] = min(depth.get(child, 10**9), depth.get(parent, 0) + 1)
            # Ids are known only at the end: the edge keeps its parent key until then.
            edges[(parent, index)] = (child, ResolvedEdge(
                source="", target="", constraint=constraint, dependency_type=want_type,
                details={"rust_cfg": rust_cfg} if rust_cfg else {}))
            grown = features.get(child, frozenset()) | asked
            if grown != features.get(child):
                features[child] = grown
                todo.append(child)

        depth[ROOT] = 0
        for want in self.root_wants:
            reach(ROOT, want.index, "runtime", want.constraint, want.features, None)
        while todo:
            key = todo.pop(0)
            activation = state.activations[key]
            deps = self.deps.get((self.refs[key], activation.version.version))
            if deps is None:
                continue
            is_root = any(state.chosen.get((ROOT, w.index)) == key for w in self.root_wants)
            switched = enabled(activation.version.features or {}, deps, features[key],
                               include_dev=self.request.include_dev and is_root)
            for index in sorted(switched.entries):
                dep = deps[index]
                rust_cfg = _rust_cfg(dep)
                if all_targets or platform_matches(rust_cfg, self.env.target):
                    reach(key, index, dep.dependency_type, dep.constraint, _asked_of(dep, switched.asks), rust_cfg)

        ids = {key: f"n{number}" for number, key in enumerate(sorted(features, key=lambda k: (depth[k], k)), start=1)}
        roots = {state.chosen.get((ROOT, w.index)) for w in self.root_wants}
        for key, node_id in ids.items():
            activation = state.activations[key]
            latest = None
            if key in roots:
                newest = [v for v in await self.candidates(self.refs[key])
                          if v.status == "active" and not _SCHEME.is_prerelease(v.version)]
                latest = newest[0].version if newest else None
            self.graph.nodes.append(ResolvedNode(
                id=node_id, ref=self.refs[key], version=activation.version.version,
                registry_slug=activation.version.registry_slug, depth=depth[key], errors=list(self.errors.get(key, [])),
                latest_compatible=latest, details={"features": sorted(features[key])},
            ))
        self.graph.edges = [replace(edge, source="root" if parent == ROOT else ids[parent], target=ids[child])
                            for (parent, _), (child, edge) in edges.items()]
        for message in self.errors.get(ROOT, []):
            self.graph.warnings.append({"code": "unresolvable", "message": message})
        for key, messages in self.errors.items():
            if key in ids and UNKNOWN_DEPENDENCIES in messages:
                self.graph.warnings.append({"code": "dependencies_unknown", "node": ids[key],
                                            "message": f"dependencies of {key[0]} "
                                                       f"{state.activations[key].version.version} are unknown"})


def _rust_cfg(dep: NormalizedDependency) -> str | None:
    value = (dep.environment_markers or {}).get("rust_cfg")
    return value if isinstance(value, str) and value else None


class CratesResolver:
    emulates = "Cargo 1.86 (resolver v2)"
    strategy = STRATEGY

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            chosen = await state.choose()
            missing_roots = [w.ref.name for w in state.root_wants if (ROOT, w.index) not in chosen.chosen
                             and not state.versions.get(w.ref)]
            if missing_roots:
                state.graph.error = {"code": "not_found",
                                     "message": f"no source of the profile has {', '.join(missing_roots)}",
                                     "packages": missing_roots}
                return state.graph
            await state.graph_for_target(chosen)
        except ResolveLimitExceeded as exc:
            state.graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit,
                                 "value": exc.value, "packages_seen": len(state.versions), "lazy_fetches": state.fetches}
        except _Aborted as exc:
            state.graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(state.versions)}
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            state.graph.error = {"code": "upstream_error", "message": f"{type(exc).__name__}: {exc}",
                                 "packages_seen": len(state.versions)}
        return state.graph


RESOLVER = CratesResolver()
