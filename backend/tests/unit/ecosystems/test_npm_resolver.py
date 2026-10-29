"""The npm resolver (IMPL F5, SPEC §9.3) over an in-memory catalog."""

import asyncio
from collections.abc import Sequence

from app.ecosystems.base import (
    CatalogVersion,
    NormalizedDependency,
    PackageRef,
    ResolveGraph,
    ResolveRequest,
    ResolveRoot,
    SourceProfile,
)
from app.ecosystems.npm.environment import parse_environment
from app.ecosystems.npm.resolver import NOT_FOUND_PREFIX, RESOLVER, UNKNOWN_DEPENDENCIES

PROFILE = SourceProfile(ecosystem="npm", slug="default", registries=[])


def _ref(name: str) -> PackageRef:
    if name.startswith("@"):
        scope, _, local = name[1:].partition("/")
        return PackageRef("npm", scope, local)
    return PackageRef("npm", "", name)


def _v(version: str, *tags: str, deprecated: bool = False, engines: dict[str, str] | None = None) -> CatalogVersion:
    return CatalogVersion(version=version, registry_id="r", registry_slug="npmjs", status="active", engines=engines,
                          tags=tags, deprecated=deprecated)


def _dep(name: str, constraint: str, kind: str = "runtime", alias: str | None = None, **details: object) -> NormalizedDependency:
    return NormalizedDependency(target=_ref(name), constraint=constraint, dependency_type=kind,  # type: ignore[arg-type]
                                raw_spec=f"{alias or name}@{constraint}", alias=alias, details=details or None)


class Catalog:
    def __init__(self, versions: dict[str, list[CatalogVersion]], deps: dict[str, list[NormalizedDependency] | None]):
        self.data = {_ref(name): v for name, v in versions.items()}
        self.deps = deps

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return self.data.get(ref, [])

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        name = f"@{ref.namespace}/{ref.name}" if ref.namespace else ref.name
        return self.deps.get(f"{name}@{version}", [])

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        return False


def _resolve(catalog: Catalog, *roots: tuple[str, str], env: dict | None = None, **limits: object) -> ResolveGraph:
    request = ResolveRequest(roots=[ResolveRoot(ref=_ref(n), constraint=c) for n, c in roots],
                             environment=parse_environment(env or {}), profile=PROFILE, **limits)  # type: ignore[arg-type]
    return asyncio.run(RESOLVER.resolve(request, catalog))


def _set(graph: ResolveGraph) -> set[tuple[str, str]]:
    assert graph.error is None, graph.error
    return {(f"@{n.ref.namespace}/{n.ref.name}" if n.ref.namespace else n.ref.name, n.version) for n in graph.nodes}


def test_latest_tag_wins_when_it_satisfies_else_the_highest() -> None:
    catalog = Catalog({"a": [_v("1.0.0"), _v("1.5.0", "latest"), _v("1.9.0"), _v("2.0.0-beta.1", "next")]}, {})
    assert _set(_resolve(catalog, ("a", "^1.0.0"))) == {("a", "1.5.0")}
    assert _set(_resolve(catalog, ("a", "~1.9.0"))) == {("a", "1.9.0")}
    assert _set(_resolve(catalog, ("a", "next"))) == {("a", "2.0.0-beta.1")}
    assert _set(_resolve(catalog, ("a", "*"))) == {("a", "1.5.0")}


def test_deprecated_and_engine_incompatible_versions_are_avoided() -> None:
    catalog = Catalog({"a": [_v("1.0.0"), _v("1.1.0", deprecated=True), _v("1.2.0", engines={"node": ">=22"}),
                             _v("1.3.0", "latest", deprecated=True)]}, {})
    assert _set(_resolve(catalog, ("a", "^1.0.0"), env={"node_version": "20.10.0"})) == {("a", "1.0.0")}
    assert _set(_resolve(catalog, ("a", "^1.0.0"), env={"node_version": "22.1.0"})) == {("a", "1.2.0")}


def test_compatible_versions_are_deduplicated_and_conflicts_nest() -> None:
    catalog = Catalog(
        {"app-a": [_v("1.0.0")], "app-b": [_v("1.0.0")], "lib": [_v("1.4.0"), _v("2.1.0", "latest")]},
        {"app-a@1.0.0": [_dep("lib", "^1.0.0")], "app-b@1.0.0": [_dep("lib", "^1.2.0")]},
    )
    graph = _resolve(catalog, ("app-a", "1"), ("app-b", "1"))
    assert _set(graph) == {("app-a", "1.0.0"), ("app-b", "1.0.0"), ("lib", "1.4.0")}   # one lib, hoisted
    # The root asks for lib@2: the 1.x copy nests under the apps that need it, one per folder.
    graph = _resolve(catalog, ("lib", "2"), ("app-a", "1"), ("app-b", "1"))
    assert sorted(_set(graph)) == [("app-a", "1.0.0"), ("app-b", "1.0.0"), ("lib", "1.4.0"), ("lib", "2.1.0")]
    assert [n.version for n in graph.nodes if n.ref.name == "lib"].count("1.4.0") == 2


def test_peer_goes_next_to_its_package_and_a_mismatch_is_a_conflict() -> None:
    catalog = Catalog(
        {"plugin": [_v("1.0.0")], "host": [_v("2.0.0"), _v("3.0.0", "latest")], "opt": [_v("1.0.0")]},
        {"plugin@1.0.0": [_dep("host", "^2.0.0", "peer"), _dep("opt", "*", "peer", optional=True)]},
    )
    assert _set(_resolve(catalog, ("plugin", "1"))) == {("plugin", "1.0.0"), ("host", "2.0.0")}   # optional peer: no
    graph = _resolve(catalog, ("host", "3"), ("plugin", "1"))
    assert graph.conflicts and graph.conflicts[0]["kind"] == "peer"
    assert any("peer host" in e for n in graph.nodes for e in n.errors)


def test_optional_dependencies_follow_os_and_cpu() -> None:
    catalog = Catalog({"chokidar": [_v("3.6.0")], "fsevents": [_v("2.3.3", engines={"os": "darwin"})]},
                      {"chokidar@3.6.0": [_dep("fsevents", "~2.3.2", "optional")]})
    assert _set(_resolve(catalog, ("chokidar", "3"), env={"os": "linux"})) == {("chokidar", "3.6.0")}
    assert _set(_resolve(catalog, ("chokidar", "3"), env={"os": "darwin"})) == {("chokidar", "3.6.0"), ("fsevents", "2.3.3")}


def test_alias_and_unresolvable_specs() -> None:
    catalog = Catalog({"cliui": [_v("8.0.2")], "string-width": [_v("4.2.3"), _v("5.1.2", "latest")]},
                      {"cliui@8.0.2": [_dep("string-width", "^4.2.0", alias="string-width-cjs"),
                                       _dep("string-width", "^5.1.2"),
                                       _dep("forked", "", unresolvable="github:me/forked")]})
    graph = _resolve(catalog, ("cliui", "8"))
    assert _set(graph) == {("cliui", "8.0.2"), ("string-width", "4.2.3"), ("string-width", "5.1.2")}
    cliui = next(n for n in graph.nodes if n.ref.name == "cliui")
    assert any("unresolvable dependency forked" in e for e in cliui.errors)
    assert any(e.details.get("alias") == "string-width-cjs" for e in graph.edges)


def test_unknown_and_missing_dependencies_give_a_partial_graph() -> None:
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")]}, {"a@1.0.0": [_dep("ghost", "^1"), _dep("b", "1")],
                                                               "b@1.0.0": None})
    graph = _resolve(catalog, ("a", "1"))
    nodes = {n.ref.name: n for n in graph.nodes}
    assert nodes["a"].errors == [f"{NOT_FOUND_PREFIX}ghost"] and nodes["b"].errors == [UNKNOWN_DEPENDENCIES]
    assert _resolve(catalog, ("ghost", "*")).error["code"] == "not_found"


def test_depth_is_the_distance_in_the_graph_and_limits_apply() -> None:
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")], "c": [_v("1.0.0")]},
                      {"a@1.0.0": [_dep("b", "1")], "b@1.0.0": [_dep("c", "1")]})
    graph = _resolve(catalog, ("a", "1"))
    assert {n.ref.name: n.depth for n in graph.nodes} == {"a": 1, "b": 2, "c": 3}   # all hoisted to the root folder
    assert graph.nodes[0].latest_compatible == "1.0.0"
    assert _resolve(catalog, ("a", "1"), max_nodes=2).error["code"] == "resolve_limit_exceeded"


def test_catalog_failure_is_a_graph_error() -> None:
    class Broken(Catalog):
        async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
            raise ConnectionError("registry went away")

    graph = _resolve(Broken({}, {}), ("a", "1"))
    assert graph.error["code"] == "upstream_error" and "registry went away" in graph.error["message"]
