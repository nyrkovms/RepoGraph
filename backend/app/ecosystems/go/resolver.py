"""Go resolve: the build list `go list -m all` gives for a main module requiring the roots (SPEC-MULTI-ECOSYSTEM.md
§9.5, Q23).

The module graph is read the way Go 1.17+ reads it (modload `readModGraph`): the main module (go 1.23) is pruned, so
each root's go.mod is read; a module whose go.mod says `go >= 1.17` contributes its requirements but their go.mod files
are not read; below a module on an older go (or one reached that way) every go.mod is read. Minimal version selection
then takes, for every module path, the highest version any read go.mod (or the main module) requires, whether or not
the module that requires it ends up selected. When MVS raises a root above the version asked for, the graph is read
again from the selected version, as Go's `expandGraph` does, until every root is selected.

go.mod files come through the catalog's document channel (source_documents); neither they nor the build list enter the
catalog (Q24). `replace` and `exclude` are the main module's, from the environment; a requirement on an excluded
version is dropped, as Go does since 1.16. A go.mod that declares another module path is reported on its node.
"""

import time
from collections import deque
from dataclasses import dataclass, field

from app.ecosystems.base import (
    CatalogAccess,
    CatalogVersion,
    InvalidVersion,
    PackageRef,
    ResolvedEdge,
    ResolvedNode,
    ResolveGraph,
    ResolveLimitExceeded,
    ResolveRequest,
)
from app.ecosystems.go.coordinates import escape_path, escape_version
from app.ecosystems.go.environment import GoEnvironment
from app.ecosystems.go.gomod import GoMod, GoModError, parse_go_mod
from app.ecosystems.go.versions import GoVersionScheme, compare

STRATEGY = "go-mvs"
MAX_EXPANSIONS = 20
NO_GO_MOD = "go.mod not found"
_SCHEME = GoVersionScheme()

Module = tuple[str, str]   # (path, version)


class _Aborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ref(path: str) -> PackageRef:
    return PackageRef("go", "", path)


def _go_at_least(go: str | None, wanted: str) -> bool:
    """`go 1.21.3` >= `1.17`; a go.mod without a `go` line counts as go 1.16."""
    def parts(text: str) -> tuple[int, ...]:
        numbers: list[int] = []
        for piece in text.split(".")[:3]:
            digits = "".join(ch for ch in piece if ch.isdigit())
            numbers.append(int(digits) if digits else 0)
        return tuple(numbers + [0] * (3 - len(numbers)))   # `1.23` is `1.23.0`

    return parts(go or "1.16") >= parts(wanted)


@dataclass(slots=True)
class _Read:
    mod: GoMod | None
    errors: list[str] = field(default_factory=list)


class _Resolve:
    def __init__(self, request: ResolveRequest, catalog: CatalogAccess) -> None:
        self.request = request
        self.catalog = catalog
        environment = request.environment
        self.env = environment if isinstance(environment, GoEnvironment) else GoEnvironment()
        self.deadline = time.monotonic() + request.timeout_s
        self.graph = ResolveGraph(strategy=STRATEGY, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        self.reads: dict[Module, _Read] = {}
        self.requires: dict[Module, list[tuple[Module, bool]]] = {}   # read module -> (required module, indirect)
        self.selected: dict[str, str] = {}
        self.listed: dict[str, list[CatalogVersion]] = {}
        self.documents = 0

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Aborted("resolve_timeout", f"resolve took longer than {self.request.timeout_s:g} s")

    async def versions(self, path: str) -> list[CatalogVersion]:
        """The versions the profile lists for a module, fetched into the catalog once (roots and exclusions only)."""
        if path not in self.listed:
            ref = _ref(path)
            versions = list(await self.catalog.versions(ref, self.request.profile))
            if not versions and await self.catalog.ensure(ref, self.request.profile):
                self.graph.fetched.append(ref)
                versions = list(await self.catalog.versions(ref, self.request.profile))
            self.listed[path] = versions
        return self.listed[path]

    async def go_mod(self, module: Module) -> _Read:
        if module not in self.reads:
            self._check()
            if self.documents >= self.request.max_lazy_fetches:
                raise ResolveLimitExceeded("lazy_fetches", self.request.max_lazy_fetches)
            self.documents += 1
            path, version = self.env.replacement(*module) or module
            document = await self.catalog.document(
                _ref(module[0]), f"{escape_path(path)}/@v/{escape_version(version)}.mod", self.request.profile)
            if document is None:
                self.reads[module] = _Read(None, [NO_GO_MOD])
                return self.reads[module]
            try:
                mod = parse_go_mod(document.content.decode("utf-8", "replace"))
            except GoModError as exc:
                self.reads[module] = _Read(None, [f"go.mod cannot be read: {exc}"])
                return self.reads[module]
            if mod.module not in (module[0], path):
                # Go refuses such a module ("declares its path as"); a replacement may declare either path.
                self.reads[module] = _Read(None, [f"go.mod declares its path as {mod.module}"])
                return self.reads[module]
            read = _Read(mod)
            for require in mod.requires:
                if not _SCHEME.is_valid(require.version):
                    read.errors.append(f"{require.path} {require.version} is not a module version")
            self.reads[module] = read
        return self.reads[module]

    def _select(self, path: str, version: str) -> None:
        current = self.selected.get(path)
        if current is None or compare(version, current) > 0:
            self.selected[path] = version

    async def read_graph(self, roots: list[Module]) -> None:
        self.selected, self.requires = {}, {}
        excluded = set(self.env.exclude)
        loaded: set[tuple[Module, bool]] = set()   # (module, read unpruned)
        queue: deque[tuple[Module, bool]] = deque((root, False) for root in roots)   # the main module is pruned
        for path, version in roots:
            self._select(path, version)
        while queue:
            module, unpruned = queue.popleft()
            if (module, unpruned) in loaded:
                continue
            loaded.add((module, unpruned))
            read = await self.go_mod(module)
            if read.mod is None:
                continue
            requires: list[tuple[Module, bool]] = []
            for require in read.mod.requires:
                if not _SCHEME.is_valid(require.version) or (require.path, require.version) in excluded:
                    continue   # an excluded version is dropped, not replaced by another (Go >= 1.16)
                requires.append(((require.path, require.version), require.indirect))
                self._select(require.path, require.version)
            self.requires[module] = requires
            if len(self.selected) > self.request.max_nodes:
                raise ResolveLimitExceeded("nodes", self.request.max_nodes)
            # A module on go >= 1.17 lists everything its build needs: what it requires is not read further, unless
            # we are already below an unpruned module.
            if unpruned or not read.mod.pruned:
                queue.extend((required, True) for required, _ in requires)

    async def build(self, roots: list[Module]) -> None:
        ids = {path: f"n{number}" for number, path in enumerate(sorted(self.selected), start=1)}
        depth: dict[str, int] = {}
        frontier = deque(path for path, _ in roots)
        for path in frontier:
            depth[path] = 1
        while frontier:
            path = frontier.popleft()
            for (required, _), _indirect in self.requires.get((path, self.selected[path]), []):
                if required not in depth:
                    depth[required] = depth[path] + 1
                    frontier.append(required)
        root_paths = {path for path, _ in roots}
        for path, version in sorted(self.selected.items()):
            read = self.reads.get((path, version))
            errors = list(read.errors) if read else []
            latest = None
            if path in root_paths:
                latest = _latest(await self.versions(path))
            listed = next((v for v in self.listed.get(path, []) if v.version == version), None)
            if listed is None and self.request.profile.registries:
                slug = self.request.profile.registries[0].slug
            else:
                slug = listed.registry_slug if listed else ""
            self.graph.nodes.append(ResolvedNode(
                id=ids[path], ref=_ref(path), version=version, registry_slug=slug, depth=depth.get(path, 1),
                errors=errors, latest_compatible=latest,
                details={"replaced_by": "@".join(r)} if (r := self.env.replacement(path, version)) else {},
            ))
            if read and read.mod and read.mod.go and not _go_at_least(self.env.go_version, read.mod.go):
                self.graph.warnings.append({"code": "go_version", "node": ids[path],
                                            "message": f"{path} {version} needs go {read.mod.go}"})
        for path, _ in roots:
            self.graph.edges.append(ResolvedEdge(source="root", target=ids[path], constraint=self.selected[path],
                                                 dependency_type="runtime"))
        # Edges of the selected versions only: what the build list is made of, not every go.mod MVS read.
        for path, version in self.selected.items():
            for (required, wanted), indirect in self.requires.get((path, version), []):
                self.graph.edges.append(ResolvedEdge(source=ids[path], target=ids[required], constraint=wanted,
                                                     dependency_type="runtime",
                                                     details={"indirect": True} if indirect else {}))


class GoResolver:
    emulates = "Go 1.23 (MVS with module graph pruning)"
    strategy = STRATEGY

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        state = _Resolve(request, catalog)
        try:
            roots: list[Module] = []
            missing: list[str] = []
            for root in request.roots:
                version = await _root_version(state, root.ref.name, root.constraint)
                if version is None:
                    missing.append(root.ref.name)
                else:
                    roots.append((root.ref.name, version))
            if missing:
                state.graph.error = {"code": "not_found", "packages": missing,
                                     "message": f"no source of the profile has a version of {', '.join(missing)}"}
                return state.graph
            # Go's expandGraph: a root MVS raised is read again from the selected version, until none is raised.
            for _ in range(MAX_EXPANSIONS):
                await state.read_graph(roots)
                selected = [(path, state.selected[path]) for path, _ in roots]
                if selected == roots:
                    break
                roots = selected
            await state.build(roots)
        except ResolveLimitExceeded as exc:
            state.graph.error = {"code": "resolve_limit_exceeded", "message": str(exc), "limit": exc.limit,
                                 "value": exc.value, "packages_seen": len(state.selected),
                                 "lazy_fetches": state.documents}
        except _Aborted as exc:
            state.graph.error = {"code": exc.code, "message": str(exc), "packages_seen": len(state.selected)}
        except Exception as exc:  # noqa: BLE001 - an upstream or database failure ends the resolve, not the request
            state.graph.error = {"code": "upstream_error", "message": f"{type(exc).__name__}: {exc}",
                                 "packages_seen": len(state.selected)}
        return state.graph


async def _root_version(state: _Resolve, path: str, constraint: str) -> str | None:
    """A version written as the root's constraint is required as such (MVS may raise it); `*` or nothing takes the
    latest release the profile lists, else the latest pseudo-version (what `go get path@latest` does)."""
    try:
        _SCHEME.validate(constraint)
        return constraint
    except InvalidVersion:
        pass
    return _latest(await state.versions(path), prereleases=True)


def _latest(versions: list[CatalogVersion], prereleases: bool = False) -> str | None:
    """The version the proxy's `@latest` names (the `latest` tag the adapter stores): it skips +incompatible tags above
    a module's real latest; without the tag, the highest active release (else prerelease, when allowed)."""
    tagged = next((v.version for v in versions if "latest" in v.tags), None)
    if tagged is not None:
        return tagged
    active = [v.version for v in versions if v.status == "active"]
    releases = [v for v in active if not _SCHEME.is_prerelease(v)]
    pool = releases or (active if prereleases else [])
    return max(pool, key=_SCHEME.sort_key) if pool else None


RESOLVER = GoResolver()
