"""The Maven resolver (IMPL J5, SPEC §9.7) over an in-memory catalog."""

import asyncio
from collections.abc import Sequence

from app.ecosystems.base import (
    CatalogVersion,
    FetchedDocument,
    NormalizedDependency,
    PackageRef,
    ResolveGraph,
    ResolveRequest,
    ResolveRoot,
    SourceProfile,
)
from app.ecosystems.maven.environment import parse_environment
from app.ecosystems.maven.resolver import RESOLVER, UNKNOWN_DEPENDENCIES, derived_scope, widest

PROFILE = SourceProfile(ecosystem="maven", slug="default", registries=[])


def _ref(name: str) -> PackageRef:
    return PackageRef("maven", "g", name)


def _v(*versions: str) -> list[CatalogVersion]:
    return [CatalogVersion(version=v, registry_id="r", registry_slug="maven-central", status="active") for v in versions]


def _dep(name: str, version: str, scope: str = "compile", optional: bool = False,
         exclusions: Sequence[tuple[str, str]] = ()) -> NormalizedDependency:
    details = {"exclusions": [{"group": g, "artifact": a} for g, a in exclusions]} if exclusions else None
    kind = "optional" if optional else {"test": "dev", "provided": "provided", "system": "provided"}.get(scope, "runtime")
    return NormalizedDependency(target=_ref(name), constraint=version, dependency_type=kind,  # type: ignore[arg-type]
                                raw_spec=f"g:{name}:{version}", scope=scope, details=details)


class Catalog:
    def __init__(self, versions: dict[str, list[CatalogVersion]],
                 deps: dict[str, list[NormalizedDependency] | None] | None = None) -> None:
        self.data = versions
        self.deps = deps or {}

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return self.data.get(ref.name, [])

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        return self.deps.get(f"{ref.name}@{version}", [])

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        return False

    async def document(self, ref: PackageRef, path: str, profile: SourceProfile) -> FetchedDocument | None:
        return None


def _resolve(catalog: Catalog, *roots: tuple[str, str], env: dict | None = None,
             managed: Sequence[tuple[str, str, str | None]] = (), strategy: str | None = None) -> ResolveGraph:
    request = ResolveRequest(
        roots=[ResolveRoot(ref=_ref(n), constraint=c) for n, c in roots],
        environment=parse_environment(env or {}), profile=PROFILE, strategy=strategy,
        managed=[ResolveRoot(ref=_ref(n), constraint=c, qualifiers={"scope": s} if s else {}) for n, c, s in managed],
    )
    return asyncio.run(RESOLVER.resolve(request, catalog))


def _set(graph: ResolveGraph) -> set[tuple[str, str]]:
    assert graph.error is None, graph.error
    return {(n.ref.name, n.version) for n in graph.nodes}


def _scopes(graph: ResolveGraph) -> dict[str, object]:
    return {n.ref.name: n.details["scope"] for n in graph.nodes}


def test_nearest_wins_and_the_first_declaration_at_one_depth() -> None:
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "p": _v("1"), "q": _v("1"), "b": _v("1.0", "2.0"),
                       "c": _v("1.0", "2.0")},
                      {"app@1": [_dep("x", "1"), _dep("b", "1.0"), _dep("p", "1"), _dep("q", "1")],
                       "x@1": [_dep("b", "2.0")],          # deeper: loses although higher
                       "p@1": [_dep("c", "1.0")], "q@1": [_dep("c", "2.0")]})   # same depth: the first declared
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1"), ("x", "1"), ("b", "1.0"), ("p", "1"), ("q", "1"), ("c", "1.0")}
    assert graph.strategy == "maven-nearest-wins" and RESOLVER.emulates == "Maven 3.9"
    assert {n.ref.name: n.depth for n in graph.nodes}["c"] == 3


def test_the_subtree_of_a_losing_occurrence_is_not_walked() -> None:
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "b": _v("1.0", "2.0"), "z": _v("1")},
                      {"app@1": [_dep("b", "1.0"), _dep("x", "1")], "x@1": [_dep("b", "2.0")],
                       "b@2.0": [_dep("z", "1")]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("b", "1.0"), ("x", "1")}


def test_managed_versions_and_scopes_apply_below_the_roots_only() -> None:
    catalog = Catalog({"app": _v("1", "2"), "b": _v("1.0", "3.0"), "c": _v("1")},
                      {"app@1": [_dep("b", "1.0"), _dep("c", "1")]})
    graph = _resolve(catalog, ("app", "1"), managed=[("b", "3.0", None), ("app", "2", None), ("c", "1", "test")])
    assert _set(graph) == {("app", "1"), ("b", "3.0")}        # c managed to test scope: not on the runtime classpath
    graph = _resolve(catalog, ("app", "1"), env={"scopes": ["compile", "runtime", "test"]}, managed=[("c", "1", "test")])
    assert _scopes(graph)["c"] == "test"


def test_a_range_takes_the_highest_available_and_a_soft_version_must_exist() -> None:
    catalog = Catalog({"app": _v("1"), "b": _v("1.0", "1.5", "2.0"), "c": _v("1.0")},
                      {"app@1": [_dep("b", "[1.0,2.0)"), _dep("c", "9.9")]})
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1"), ("b", "1.5")}
    assert graph.warnings == [{"code": "unresolvable", "message": "no version of g:c matches 9.9 (required by g:app)"}]


def test_optional_test_and_provided_dependencies_are_not_transitive_and_exclusions_accumulate() -> None:
    catalog = Catalog({n: _v("1") for n in ("app", "x", "y", "z", "o", "t", "p", "s")},
                      {"app@1": [_dep("x", "1", exclusions=[("g", "z")])],
                       "x@1": [_dep("y", "1"), _dep("o", "1", optional=True), _dep("t", "1", "test"),
                               _dep("p", "1", "provided"), _dep("s", "1", "system")],
                       "y@1": [_dep("z", "1")]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("x", "1"), ("y", "1")}
    catalog.deps["app@1"] = [_dep("x", "1", exclusions=[("*", "*")])]
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("x", "1")}


def test_scopes_are_derived_and_the_widest_occurrence_wins() -> None:
    catalog = Catalog({n: _v("1") for n in ("app", "r", "s", "d", "e", "c")},
                      {"app@1": [_dep("r", "1", "runtime"), _dep("d", "1")],
                       "r@1": [_dep("s", "1"), _dep("c", "1")],          # below runtime: runtime
                       "d@1": [_dep("e", "1")], "e@1": [_dep("c", "1")]})  # a deeper compile occurrence widens c
    graph = _resolve(catalog, ("app", "1"))
    assert _scopes(graph) == {"app": "compile", "r": "runtime", "d": "compile", "s": "runtime", "e": "compile",
                              "c": "compile"}
    assert {n.ref.name: n.version for n in _resolve(catalog, ("app", "1"), env={"scopes": "compile"}).nodes}.keys() \
        == {"app", "d", "e", "c"}
    assert derived_scope("provided", "runtime") == "provided" and derived_scope("runtime", "test") == "test"
    assert widest({"system", "test"}) == "test" and widest({"system"}) == "system"


def test_edges_and_errors() -> None:
    catalog = Catalog({"app": _v("1", "2", "3-RC1"), "b": _v("1")}, {"app@1": [_dep("b", "1")], "b@1": None})
    graph = _resolve(catalog, ("app", "1"))
    nodes = {n.ref.name: n for n in graph.nodes}
    assert nodes["b"].errors == [UNKNOWN_DEPENDENCIES] and nodes["app"].latest_compatible == "2"
    assert [(e.source, e.target, e.constraint) for e in graph.edges] == [("root", "n1", "1"), ("n1", "n2", "1")]
    missing = _resolve(catalog, ("nope", "1"))
    assert missing.error is not None and missing.error["code"] == "not_found"


def test_gradle_takes_the_highest_version_wherever_declared() -> None:
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "b": _v("1.0", "2.0"), "z": _v("1")},
                      {"app@1": [_dep("b", "1.0"), _dep("x", "1")], "x@1": [_dep("b", "2.0")],
                       "b@2.0": [_dep("z", "1")]})
    graph = _resolve(catalog, ("app", "1"), strategy="gradle")
    assert _set(graph) == {("app", "1"), ("x", "1"), ("b", "2.0"), ("z", "1")}
    assert graph.strategy == "gradle-highest-wins"


def test_environment_parsing() -> None:
    env = parse_environment({"scopes": "compile, test", "strategy": "gradle", "java_version": 21})
    assert env.scopes == ("compile", "test") and env.strategy == "gradle" and env.java_version == "21"
    assert parse_environment({}).specified is False
    for bad in ({"scopes": ["bogus"]}, {"strategy": "ivy"}):
        try:
            parse_environment(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
