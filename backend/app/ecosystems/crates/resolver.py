"""crates resolve: the versions Cargo's resolver picks (`Cargo.lock`) and the graph for a target
(SPEC-MULTI-ECOSYSTEM.md §9.4).

Pass 1 chooses versions the way Cargo's dependency resolver does: one activation per (crate, semver-compatibility
class), so `rand 0.7` and `rand 0.8` can live side by side; the dependency with the fewest candidates goes next;
candidates newest first, never a yanked one (Cargo refuses them in a new lock even for an `=` pin); a candidate whose
class already holds another version, that lacks a feature asked of it, or whose `links` another activation has, is
skipped. A dependency left without candidates jumps back to the latest choice among those that caused its refusals
(the activations in the way, the ones asking, and what brought those in), as Cargo's conflict-driven backjumping does,
and fails at once when no choice can help. Features asked of an activation by all its dependents are unified, and optional dependencies
come in through them (`dep:x`, `x/feat`, a bare `x`; `x?/feat` does not turn `x` on). Like Cargo.lock it ignores the
platform: dependencies of every target take part.

`options.include_optional` has no meaning here: optional dependencies come with features, as in Cargo. A dependency from
another registry (`registry = "..."`) is not in the profile and is reported on its dependent.

Pass 2 walks the chosen versions from the roots again, following only the dependencies that apply on the target and
recomputing features along those edges, which is what `cargo tree --target <t>` shows. Target `*` is Cargo.lock:
every platform, and weak features bring their dependency in as pass 1 does.

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
from app.ecosystems.crates.versions import CratesVersionScheme, compatibility, parse

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
    brought_by: dict[Key, Key] = field(default_factory=dict)           # activation -> the one that first asked for it
    errors: dict[tuple[Key, str], list[str]] = field(default_factory=dict)   # (activation, version) -> node errors

    def copy(self) -> "_State":
        return _State(dict(self.activations), list(self.pending), dict(self.chosen), dict(self.brought_by),
                      {key: list(messages) for key, messages in self.errors.items()})

    def error(self, key: Key, version: str, message: str) -> None:
        messages = self.errors.setdefault((key, version), [])
        if message not in messages:
            messages.append(message)

    def askers(self, key: Key) -> set[Key]:
        return {parent for (parent, _), child in self.chosen.items() if child == key}

    def causes(self, keys: set[Key]) -> set[Key]:
        """`keys` and everything that brought them in, up to the roots."""
        out: set[Key] = set()
        todo = list(keys)
        while todo:
            key = todo.pop()
            if key not in out:
                out.add(key)
                if key in self.brought_by:
                    todo.append(self.brought_by[key])
        return out


@dataclass(frozen=True, slots=True)
class _Refusal:
    reason: str
    blocking: frozenset[Key] = frozenset()   # activations in the way


@dataclass(slots=True)
class _Frame:
    state: _State                       # before the choice
    want: _Want
    alternatives: list[CatalogVersion]
    key: Key                            # the activation the choice made


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
            include_dev: bool = False, weak_switches_on: bool = False) -> _Enabled:
    """Which dependency entries `asked` features switch on, and the features they ask of each dependency.

    `weak_switches_on`: Cargo's dependency resolver lets `x?/feat` bring `x` into Cargo.lock although no build uses it
    (rust-lang/cargo#10801); pass 1 asks for that, the build graph does not.
    """
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
            is_weak = name.endswith("?")
            name = name.removesuffix("?")
            if is_weak and not weak_switches_on:
                weak.append((name, feature))
                continue
            if name in optional:
                on.add(name)
                # A strong `x/feat` also turns on a feature named `x` if the crate has one (`x = ["dep:x", ...]`),
                # as Cargo does (rust-lang/cargo#10788, #12130).
                if not is_weak and name in features:
                    todo.append(name)
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
        """Versions the requirement accepts, newest first, never a yanked one."""
        key = (want.ref, want.constraint)
        if key not in self._matching:
            try:
                constraint = _SCHEME.constraint(want.constraint)
            except InvalidVersion:
                constraint = None
            self._matching[key] = [] if constraint is None else [
                v for v in await self.candidates(want.ref) if constraint.contains(v.version) and v.status == "active"]
        return self._matching[key]

    # --- pass 1: versions ----------------------------------------------------------------------------------------

    async def _activate(self, state: _State, want: _Want, version: CatalogVersion) -> _Refusal | None:
        """Activate `version` for `want` (or add features to its activation); why not, if it cannot take them.
        A refusal leaves the state as it was."""
        key = (want.ref.name, compatibility(version.version))
        current = state.activations.get(key)
        if current is not None and current.version.version != version.version:
            return _Refusal(f"{want.ref.name} {current.version.version} is already chosen", frozenset({key}))
        if version.links and current is None:
            taken = [k for k, a in state.activations.items() if a.version.links == version.links]
            if taken:
                return _Refusal(f"{want.ref.name} {version.version} links {version.links!r} like {taken[0][0]}",
                                frozenset(taken))
        features = (current.features if current else frozenset()) | want.features
        deps = await self.dependencies(want.ref, version)
        switched = enabled(version.features or {}, deps or [], features,
                           include_dev=self.request.include_dev and want.parent == ROOT, weak_switches_on=True)
        if switched.missing:
            # Cargo: "package does not have that feature".
            return _Refusal(f"{want.ref.name} {version.version} has no feature {', '.join(sorted(switched.missing))}",
                            frozenset({key}) if current is not None else frozenset())
        if current is not None and current.features == features:
            state.chosen[(want.parent, want.index)] = key
            return None
        if len(state.activations) + (current is None) > self.request.max_nodes:
            raise ResolveLimitExceeded("nodes", self.request.max_nodes)
        state.activations[key] = _Activation(version, features)
        state.chosen[(want.parent, want.index)] = key
        state.brought_by.setdefault(key, want.parent)
        self.refs[key] = want.ref
        if deps is None:
            state.error(key, version.version, UNKNOWN_DEPENDENCIES)
            return None
        # Its dependencies come (back) with the features now asked; ones already satisfied are reused at once.
        for index, dep in enumerate(deps):
            if index not in switched.entries:
                continue
            if (dep.details or {}).get("registry"):
                state.error(key, version.version, f"{dep.target.name} is from another registry")
                continue
            state.pending.append(_Want(key, index, dep.target, dep.constraint, _asked_of(dep, switched.asks),
                                       dep.dependency_type, _rust_cfg(dep)))
        return None

    def _causes(self, state: _State, want: _Want, refusals: Sequence[_Refusal]) -> set[Key]:
        """The activations a different choice of which could let `want` through."""
        keys = {want.parent}
        for refusal in refusals:
            for key in refusal.blocking:
                keys |= {key} | state.askers(key)
        return state.causes(keys)

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
                parent_version = state.activations[want.parent].version.version if want.parent != ROOT else ""
                state.error(want.parent, parent_version, f"no version of {want.ref.name} matches {want.constraint}")
                continue
            before = state.copy()
            refusals: list[_Refusal] = []
            for tried, version in enumerate(options):
                refusal = await self._activate(state, want, version)
                if refusal is None:
                    if tried + 1 < len(options):
                        before.pending.insert(position, want)
                        frames.append(_Frame(before, want, options[tried + 1:], (want.ref.name,
                                                                               compatibility(version.version))))
                    break
                refusals.append(refusal)
            else:
                state = await self._backjump(frames, self._causes(state, want, refusals), want, refusals)
        return state

    async def _backjump(self, frames: list[_Frame], causes: set[Key], failed: _Want,
                        refusals: list[_Refusal]) -> _State:
        while frames:
            frame = frames.pop()
            if frame.key not in causes:
                continue                       # a choice that has nothing to do with the failure
            while frame.alternatives:
                version = frame.alternatives.pop(0)
                state = frame.state.copy()
                state.pending.remove(frame.want)
                refusal = await self._activate(state, frame.want, version)
                if refusal is None:
                    frame.key = (frame.want.ref.name, compatibility(version.version))
                    if frame.alternatives:
                        frames.append(frame)
                    return state
                causes |= self._causes(frame.state, frame.want, [refusal])
            causes |= frame.state.causes({frame.want.parent})
        reasons = "; ".join(dict.fromkeys(r.reason for r in refusals))
        raise _Aborted("resolution_impossible",
                       f"no version of {failed.ref.name} ({failed.constraint}) can be chosen: {reasons}")

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
                               include_dev=self.request.include_dev and is_root, weak_switches_on=all_targets)
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
                registry_slug=activation.version.registry_slug, depth=depth[key],
                errors=list(state.errors.get((key, activation.version.version), [])),
                latest_compatible=latest, details={"features": sorted(features[key])},
            ))
        self.graph.edges = [replace(edge, source="root" if parent == ROOT else ids[parent], target=ids[child])
                            for (parent, _), (child, edge) in edges.items()]
        for message in state.errors.get((ROOT, ""), []):
            self.graph.warnings.append({"code": "unresolvable", "message": message})
        for (key, version), messages in state.errors.items():
            if key in ids and version == state.activations[key].version.version and UNKNOWN_DEPENDENCIES in messages:
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
