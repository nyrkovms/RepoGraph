"""PyPI resolve: resolvelib backtracking like pip 24 (SPEC-MULTI-ECOSYSTEM.md §9.1, §9.2).

resolvelib is synchronous, the catalog is async: the resolution runs in a worker thread and every catalog call is
sent back to the event loop that called `resolve`, so the catalog keeps using that loop's session.
"""

import asyncio
import time
from collections.abc import Coroutine, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import reduce
from operator import and_
from typing import TypeVar, cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from resolvelib import AbstractProvider, BaseReporter, Resolver
from resolvelib.resolvers import RequirementInformation, ResolutionImpossible, ResolutionTooDeep

from app.ecosystems.base import (
    CatalogAccess,
    CatalogVersion,
    DependencyType,
    NormalizedDependency,
    PackageRef,
    ResolvedEdge,
    ResolvedNode,
    ResolveGraph,
    ResolveLimitExceeded,
    ResolveRequest,
)
from app.ecosystems.pypi.environment import PythonEnvironment, _evaluate

STRATEGY = "pip-resolvelib"
MAX_ROUNDS = 2000
UNKNOWN_DEPENDENCIES = "dependencies unknown"
NOT_FOUND_PREFIX = "no source has "
_SDIST_SUFFIXES = (".tar.gz", ".zip", ".tar.bz2", ".tgz")

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Requirement:
    name: str
    specifier: SpecifierSet
    constraint: str
    extras: frozenset[str] = frozenset()
    dependency_type: str = "runtime"
    extra_name: str | None = None
    environment_markers: Mapping[str, object] | None = None

    @property
    def key(self) -> str:
        return _key(self.name, self.extras)

    @property
    def pins(self) -> bool:
        """An exact pin: the only way a yanked version may be chosen (PEP 592)."""
        return any(spec.operator in ("==", "===") and "*" not in spec.version for spec in self.specifier)


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    version: Version
    source: CatalogVersion = field(compare=False)   # one catalog version per (name, version)
    extras: frozenset[str] = frozenset()


def _key(name: str, extras: frozenset[str]) -> str:
    """resolvelib identifier. As in pip, `name[extras]` is a package of its own that depends on `name` at the same
    version: extras asked after `name` is pinned still bring their dependencies (one key would keep the pin)."""
    return f"{name}[{','.join(sorted(extras))}]" if extras else name


class _Aborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Bridge:
    """Sync calls from the resolver thread into the async catalog on the caller's loop, with the request deadline."""

    def __init__(self, loop: asyncio.AbstractEventLoop, catalog: CatalogAccess, request: ResolveRequest) -> None:
        self.loop = loop
        self.catalog = catalog
        self.request = request
        self.deadline = time.monotonic() + request.timeout_s
        self.fetches = 0
        self.fetched: list[PackageRef] = []
        self.task: asyncio.Task[object] | None = None   # the last catalog call; resolve() awaits it before returning

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s")

    def _call(self, coroutine: Coroutine[object, object, T]) -> T:
        try:
            self.check()
        except _Aborted:
            coroutine.close()
            raise
        result: Future[T] = Future()

        def start() -> None:
            task = self.loop.create_task(coroutine)
            self.task = cast("asyncio.Task[object]", task)
            task.add_done_callback(lambda done: _settle(done, result))

        # Our own task, not run_coroutine_threadsafe: cancelling that future marks it done at once, while the task
        # still unwinds on the caller's session. Callbacks run in order, so a cancel below always finds the task.
        self.loop.call_soon_threadsafe(start)
        try:
            return result.result(timeout=max(0.0, self.deadline - time.monotonic()))
        except TimeoutError:
            self.loop.call_soon_threadsafe(lambda: self.task.cancel() if self.task else None)
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s") from None
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            raise _Aborted("upstream_error", f"{type(exc).__name__}: {exc}") from exc

    def versions(self, ref: PackageRef) -> Sequence[CatalogVersion]:
        return self._call(self.catalog.versions(ref, self.request.profile))

    def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        return self._call(self.catalog.dependencies(ref, version, registry_id))

    def ensure(self, ref: PackageRef) -> bool:
        if self.fetches >= self.request.max_lazy_fetches:
            raise ResolveLimitExceeded("lazy_fetches", self.request.max_lazy_fetches)
        self.fetches += 1
        found = self._call(self.catalog.ensure(ref, self.request.profile))
        if found:
            self.fetched.append(ref)
        return found


def _settle(task: "asyncio.Task[T]", result: Future[T]) -> None:
    if task.cancelled():
        result.cancel()
    elif (error := task.exception()) is not None:
        result.set_exception(error)
    else:
        result.set_result(task.result())


class _Provider(AbstractProvider[_Requirement, _Candidate, str]):
    def __init__(self, bridge: _Bridge, environment: PythonEnvironment, include_dev: bool) -> None:
        self.bridge = bridge
        self.environment = environment
        self.include_dev = include_dev
        self.max_nodes = bridge.request.max_nodes
        self._versions: dict[str, list[_Candidate]] = {}
        self._dependencies: dict[tuple[str, str, frozenset[str]], list[_Requirement]] = {}
        self.unknown_dependencies: set[tuple[str, str]] = set()
        self.unresolvable: dict[tuple[str, str], list[str]] = {}   # (name, version) -> dependencies no source has
        self.missing: set[str] = set()

    def identify(self, requirement_or_candidate: _Requirement | _Candidate) -> str:
        return _key(requirement_or_candidate.name, requirement_or_candidate.extras)

    def get_preference(
        self,
        identifier: str,
        resolutions: Mapping[str, _Candidate],
        candidates: Mapping[str, Iterator[_Candidate]],
        information: Mapping[str, Iterator[RequirementInformation[_Requirement, _Candidate]]],
        backtrack_causes: Sequence[RequirementInformation[_Requirement, _Candidate]],
    ) -> int:
        return sum(1 for _ in candidates[identifier])

    def find_matches(
        self,
        identifier: str,
        requirements: Mapping[str, Iterator[_Requirement]],
        incompatibilities: Mapping[str, Iterator[_Candidate]],
    ) -> list[_Candidate]:
        self.bridge.check()
        reqs = list(requirements[identifier])
        if not reqs:
            return []
        name, extras = reqs[0].name, reqs[0].extras   # one identifier: the same name and extras
        blocked = {c.version for c in incompatibilities[identifier]}
        pinned = any(req.pins for req in reqs)
        usable = [c for c in self.candidates(name) if c.version not in blocked
                  and (c.source.status == "active" or (c.source.status == "yanked" and pinned))]
        allowed = _filter(usable, reqs)
        return [replace(c, extras=extras) for c in sorted(allowed, key=lambda c: c.version, reverse=True)]

    def is_satisfied_by(self, requirement: _Requirement, candidate: _Candidate) -> bool:
        return requirement.specifier.contains(candidate.version, prereleases=True)

    def get_dependencies(self, candidate: _Candidate) -> list[_Requirement]:
        """`name`: its runtime dependencies. `name[extras]`: `name` at the same version plus the dependencies of the
        extras. A dependency no source of the profile has is left out and reported on the node (partial graph)."""
        key = (candidate.name, candidate.source.version, candidate.extras)
        if key not in self._dependencies:
            ref = PackageRef(ecosystem="pypi", namespace="", name=candidate.name)
            deps = self.bridge.dependencies(ref, candidate.source.version, candidate.source.registry_id)
            if deps is None:
                self.unknown_dependencies.add((candidate.name, candidate.source.version))
                deps = []
            reqs = [req for dep in deps if (req := self._requirement(dep, candidate.extras))
                    and (not candidate.extras or dep.dependency_type == "optional")]
            missing = [req.name for req in reqs if not self.candidates(req.name) and req.name in self.missing]
            if missing:
                self.unresolvable.setdefault((candidate.name, candidate.source.version), []).extend(missing)
                reqs = [req for req in reqs if req.name not in missing]
            if candidate.extras:
                pin = f"=={candidate.version}"
                reqs.insert(0, _Requirement(candidate.name, SpecifierSet(pin), pin))
            self._dependencies[key] = reqs
        return self._dependencies[key]

    def candidates(self, name: str) -> list[_Candidate]:
        """Versions usable in the environment, any status; loads the package (lazily fetched if unknown)."""
        if name not in self._versions:
            if len(self._versions) >= self.max_nodes:
                raise ResolveLimitExceeded("nodes", self.max_nodes)
            ref = PackageRef(ecosystem="pypi", namespace="", name=name)
            versions = self.bridge.versions(ref)
            if not versions and self.bridge.ensure(ref):
                versions = self.bridge.versions(ref)
            if not versions:
                self.missing.add(name)
            self._versions[name] = [
                _Candidate(name, parsed, v) for v in versions
                if (parsed := _parse(v.version)) is not None and self._usable(v)
            ]
        return self._versions[name]

    def latest_compatible(self, name: str) -> str | None:
        """The newest active version usable in the environment, stable before prerelease; constraints ignored."""
        active = [c for c in self.candidates(name) if c.source.status == "active"]
        stable = [c for c in active if not c.version.is_prerelease]
        best = max(stable or active, key=lambda c: c.version, default=None)
        return best.source.version if best else None

    def _usable(self, version: CatalogVersion) -> bool:
        python = (version.engines or {}).get("python")
        if python and not _python_allows(python, self.environment.python_full_version):
            return False
        return _artifacts_allow(version.artifacts, self.environment.sys_platform)

    def _requirement(self, dep: NormalizedDependency, extras: frozenset[str]) -> _Requirement | None:
        if dep.target.ecosystem != "pypi" or dep.dependency_type in ("build", "peer", "provided"):
            return None
        if dep.dependency_type == "dev" and not self.include_dev:
            return None
        markers = dep.environment_markers
        if dep.dependency_type == "optional":
            # The marker names the parent's extra (`extra == "test"`); it applies if any requested extra matches.
            if not any(_applies(markers, self.environment, extra) for extra in extras):
                return None
        elif not _applies(markers, self.environment):
            return None
        return _make_requirement(dep.target.name, dep.constraint, _requested_extras(dep.raw_spec),
                                 dep.dependency_type, dep.extra_name, markers)


class PyPIResolver:
    emulates = "pip 24 (resolvelib)"
    strategy = STRATEGY

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        loop = asyncio.get_running_loop()
        bridge = _Bridge(loop, catalog, request)
        try:
            return await loop.run_in_executor(_EXECUTOR, _resolve, request, bridge)
        finally:
            # A catalog call cancelled by the deadline may still be unwinding on the caller's session.
            if bridge.task is not None and not bridge.task.done():
                await asyncio.wait([bridge.task])


# Own threads: a resolve holds its thread while it waits for the loop, and the default executor also serves
# getaddrinfo for new database and HTTP connections, which the waiting resolves themselves need.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="resolve")


def _resolve(request: ResolveRequest, bridge: _Bridge) -> ResolveGraph:
    environment = request.environment
    if not isinstance(environment, PythonEnvironment) or not environment.specified:
        environment = PythonEnvironment()   # resolve always evaluates markers; pip's default target
    elif not environment.sys_platform:
        # Only a Python version given: the platform of the default target, as without any environment.
        default = PythonEnvironment()
        environment = replace(environment, sys_platform=default.sys_platform, os_name=default.os_name,
                              platform_system=default.platform_system)
    provider = _Provider(bridge, environment, request.include_dev)
    roots = [
        _make_requirement(root.ref.name, root.constraint, list(root.extras), "runtime", None, None)
        for root in request.roots
    ]
    graph = ResolveGraph(strategy=STRATEGY, nodes=[], edges=[], conflicts=[], warnings=[], fetched=bridge.fetched)
    try:
        mapping = Resolver(provider, BaseReporter()).resolve(roots, max_rounds=MAX_ROUNDS).mapping
    except ResolutionImpossible as exc:
        causes = cast(Iterable[RequirementInformation[_Requirement, _Candidate]], exc.causes)
        graph.conflicts = [
            {"requirement": f"{c.requirement.key}{c.requirement.specifier}",
             "parent": f"{_key(c.parent.name, c.parent.extras)}=={c.parent.source.version}" if c.parent else None}
            for c in causes
        ]
        missing = sorted(provider.missing & {root.name for root in roots})
        graph.error = (
            {"code": "not_found", "message": f"no source of the profile has {', '.join(missing)}", "packages": missing}
            if missing else {"code": "resolver_conflict", "message": "Dependency conflict"}
        )
        return graph
    except ResolutionTooDeep:
        graph.error = {"code": "resolver_conflict", "message": f"Resolution exceeded {MAX_ROUNDS} rounds"}
        return graph
    except ResolveLimitExceeded as exc:
        graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit, "value": exc.value,
                       "packages_seen": len(provider._versions), "lazy_fetches": bridge.fetches}
        return graph
    except _Aborted as exc:
        graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(provider._versions)}
        return graph

    _build_graph(graph, provider, roots, mapping)
    return graph


def _build_graph(
    graph: ResolveGraph, provider: _Provider, roots: Sequence[_Requirement], mapping: Mapping[str, _Candidate]
) -> None:
    """A node per package (its `name[extras]` identifiers merge into it), in breadth-first order from the roots
    (depth = shortest distance); an edge for every dependency."""
    by_name: dict[str, list[_Candidate]] = {}
    for candidate in mapping.values():
        by_name.setdefault(candidate.name, []).append(candidate)
    ids: dict[str, str] = {}
    depth: dict[str, int] = {}
    queue: list[str] = []
    for root in roots:
        if root.name in by_name and root.name not in ids:
            ids[root.name] = f"n{len(ids) + 1}"
            depth[root.name] = 1
            queue.append(root.name)
        graph.edges.append(_edge("root", ids.get(root.name, root.name), root))

    root_names = {root.name for root in roots}
    position = 0
    while position < len(queue):
        name = queue[position]
        position += 1
        for candidate in by_name[name]:
            for req in provider.get_dependencies(candidate):
                if req.name == name or req.name not in by_name:
                    continue   # the base pin of `name[extras]`
                if req.name not in ids:
                    ids[req.name] = f"n{len(ids) + 1}"
                    depth[req.name] = depth[name] + 1
                    queue.append(req.name)
                graph.edges.append(_edge(ids[name], ids[req.name], req))

    for name in queue:
        candidate = min(by_name[name], key=lambda c: len(c.extras))
        version = candidate.source.version
        node = ResolvedNode(
            id=ids[name], ref=PackageRef(ecosystem="pypi", namespace="", name=name), version=version,
            registry_slug=candidate.source.registry_slug, depth=depth[name],
            latest_compatible=provider.latest_compatible(name) if name in root_names else None,
        )
        if (name, version) in provider.unknown_dependencies:
            node.errors.append(UNKNOWN_DEPENDENCIES)
            graph.warnings.append({"code": "dependencies_unknown", "node": node.id,
                                   "message": f"dependencies of {name} {version} are unknown"})
        for dependency in sorted(set(provider.unresolvable.get((name, version), []))):
            node.errors.append(f"{NOT_FOUND_PREFIX}{dependency}")
            graph.warnings.append({"code": "dependency_not_found", "node": node.id, "package": dependency,
                                   "message": f"no source of the profile has {dependency}, required by {name} {version}"})
        graph.nodes.append(node)


def _edge(source: str, target: str, req: _Requirement) -> ResolvedEdge:
    details: dict[str, object] = {}
    if req.extra_name:
        details["extra_name"] = req.extra_name
    if req.environment_markers:
        details["environment_markers"] = dict(req.environment_markers)
    if req.extras:
        details["extras"] = sorted(req.extras)
    return ResolvedEdge(source=source, target=target, constraint=req.constraint,
                        dependency_type=cast(DependencyType, req.dependency_type), details=details)


def _make_requirement(
    name: str, constraint: str, extras: Sequence[str], dependency_type: str, extra_name: str | None,
    markers: Mapping[str, object] | None,
) -> _Requirement:
    raw = "" if constraint in ("", "*") else constraint
    try:
        specifier = SpecifierSet(raw)
    except InvalidSpecifier:
        specifier = SpecifierSet("")
    return _Requirement(canonicalize_name(name), specifier, constraint or "*", frozenset(extras), dependency_type,
                        extra_name, markers)


def _filter(candidates: Sequence[_Candidate], reqs: Sequence[_Requirement]) -> list[_Candidate]:
    """Candidates every requirement allows, with pip's prerelease rule: prereleases only when a specifier names one
    or nothing else matches (packaging's SpecifierSet.filter)."""
    combined = reduce(and_, (req.specifier for req in reqs), SpecifierSet(""))
    versions = set(combined.filter([c.version for c in candidates]))
    return [c for c in candidates if c.version in versions]


def _applies(markers: Mapping[str, object] | None, env: PythonEnvironment, extra: str | None = None) -> bool:
    expression = markers.get("expression") if isinstance(markers, Mapping) else None
    return not isinstance(expression, str) or not expression or _evaluate(expression, env, extra)


def _python_allows(requires_python: str, python_version: str) -> bool:
    try:
        return SpecifierSet(requires_python).contains(Version(python_version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return True


def _parse(raw: str) -> Version | None:
    try:
        return Version(raw)
    except InvalidVersion:
        return None   # invalid for PEP 440: the resolver ignores it (IMPL T15)


def _artifacts_allow(files: Sequence[str], sys_platform: str) -> bool:
    """An sdist or a pure wheel installs anywhere; otherwise some wheel must target the platform."""
    names = [name.lower() for name in files]
    if not names or any(name.endswith(_SDIST_SUFFIXES) for name in names):
        return True
    wheels = [name for name in names if name.endswith(".whl")]
    if not wheels or any(name.endswith("-none-any.whl") for name in wheels):
        return True
    markers = {"win32": ("win32", "win_amd64", "win_arm64"), "darwin": ("macosx",),
               "linux": ("manylinux", "musllinux", "linux_")}.get(sys_platform)
    return markers is None or any(marker in name for name in wheels for marker in markers)


def _requested_extras(raw_spec: str) -> list[str]:
    # Extras the parent asks of this dependency, e.g. `requests[socks]`.
    try:
        return sorted(Requirement(raw_spec).extras)
    except InvalidRequirement:
        return []


RESOLVER = PyPIResolver()
