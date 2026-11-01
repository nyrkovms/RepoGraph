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
from app.ecosystems.maven.environment import jdk_activates, parse_environment
from app.ecosystems.maven.resolver import RESOLVER, UNKNOWN_DEPENDENCIES, derived_scope, widest

PROFILE = SourceProfile(ecosystem="maven", slug="default", registries=[])


def _ref(name: str) -> PackageRef:
    return PackageRef("maven", "g", name)


def _v(*versions: str) -> list[CatalogVersion]:
    return [CatalogVersion(version=v, registry_id="r", registry_slug="maven-central", status="active") for v in versions]


def _dep(name: str, version: str, scope: str = "compile", optional: bool = False,
         exclusions: Sequence[tuple[str, str]] = (), markers: dict | None = None,
         relocation: bool = False) -> NormalizedDependency:
    details: dict | None = {"exclusions": [{"group": g, "artifact": a} for g, a in exclusions]} if exclusions else None
    if relocation:
        details = {"relocation": True}
    kind = "optional" if optional else {"test": "dev", "provided": "provided", "system": "provided"}.get(scope, "runtime")
    return NormalizedDependency(target=_ref(name), constraint=version, dependency_type=kind,  # type: ignore[arg-type]
                                raw_spec=f"g:{name}:{version}", scope=scope, details=details,
                                environment_markers=markers)


class Catalog:
    def __init__(self, versions: dict[str, list[CatalogVersion]],
                 deps: dict[str, list[NormalizedDependency] | None] | None = None,
                 documents: dict[str, str] | None = None) -> None:
        self.data = versions
        self.deps = deps or {}
        self.documents = documents or {}

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return self.data.get(ref.name, [])

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        return self.deps.get(f"{ref.name}@{version}", [])

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        return False

    async def document(self, ref: PackageRef, path: str, profile: SourceProfile) -> FetchedDocument | None:
        content = self.documents.get(path)
        return FetchedDocument(path, content.encode(), "application/xml") if content is not None else None


def _resolve(catalog: Catalog, *roots: tuple[str, str], env: dict | None = None,
             managed: Sequence[tuple[str, str, str | None]] = (), strategy: str | None = None) -> ResolveGraph:
    request = ResolveRequest(
        roots=[ResolveRoot(ref=_ref(n), constraint=c) for n, c in roots],
        environment=parse_environment(env or {}), profile=PROFILE, strategy=strategy,
        managed=[ResolveRoot(ref=_ref(n), constraint=c,
                             qualifiers={"type": "pom"} if s == "bom" else {"scope": s} if s else {})
                 for n, c, s in managed],
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


def _pom(artifact: str, version: str, managed: str = "", parent: str = "") -> str:
    return (f"<project><groupId>g</groupId><artifactId>{artifact}</artifactId><version>{version}</version>{parent}"
            f"<packaging>pom</packaging><dependencyManagement><dependencies>{managed}</dependencies>"
            "</dependencyManagement></project>")


def _managed(artifact: str, version: str) -> str:
    return f"<dependency><groupId>g</groupId><artifactId>{artifact}</artifactId><version>{version}</version></dependency>"


def test_a_bom_among_the_managed_entries_is_imported_in_place() -> None:
    catalog = Catalog({"app": _v("1"), "b": _v("1.0", "2.0", "3.0"), "c": _v("1.0", "2.0")},
                      {"app@1": [_dep("b", "1.0"), _dep("c", "1.0")]},
                      {"g/bom/5/bom-5.pom": _pom("bom", "5", _managed("b", "2.0") + _managed("c", "${c.version}"),
                                                "<parent><groupId>g</groupId><artifactId>base</artifactId>"
                                                "<version>1</version></parent>"),
                       "g/base/1/base-1.pom": _pom("base", "1").replace(
                           "<packaging>", "<properties><c.version>2.0</c.version></properties><packaging>")})
    # An entry before the BOM wins over the BOM's own; the BOM's parent gives its property.
    graph = _resolve(catalog, ("app", "1"), managed=[("b", "3.0", None), ("bom", "5", "bom")])
    assert _set(graph) == {("app", "1"), ("b", "3.0"), ("c", "2.0")}
    graph = _resolve(catalog, ("app", "1"), managed=[("bom", "6", "bom")])
    assert graph.warnings == [{"code": "unresolvable", "message": "BOM g:bom:6 not found"}]


def test_a_root_asked_for_with_a_star_takes_the_highest_release() -> None:
    catalog = Catalog({"app": _v("1.0", "2.0", "3.0-RC1")})
    assert _set(_resolve(catalog, ("app", "*"))) == {("app", "2.0")}


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


def test_gradle_drops_what_only_a_losing_version_asked_for() -> None:
    # b 1.0 (asked first) needs old 1.0; x upgrades b to 2.0, which does not: old leaves, and so does its own pick.
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "b": _v("1.0", "2.0"), "old": _v("1.0", "3.0"), "y": _v("1")},
                      {"app@1": [_dep("b", "1.0"), _dep("x", "1")], "x@1": [_dep("b", "2.0"), _dep("y", "1")],
                       "b@1.0": [_dep("old", "3.0")], "y@1": [_dep("old", "1.0")]})
    graph = _resolve(catalog, ("app", "1"), strategy="gradle")
    assert _set(graph) == {("app", "1"), ("x", "1"), ("b", "2.0"), ("y", "1"), ("old", "1.0")}


def test_gradle_excludes_a_module_only_when_every_path_does() -> None:
    catalog = Catalog({n: _v("1") for n in ("app", "p", "q", "z")},
                      {"app@1": [_dep("p", "1", exclusions=[("g", "z")]), _dep("q", "1")],
                       "p@1": [_dep("z", "1")], "q@1": [_dep("z", "1")]})
    assert ("z", "1") in _set(_resolve(catalog, ("app", "1"), strategy="gradle"))
    catalog.deps["app@1"] = [_dep("p", "1", exclusions=[("g", "z")]), _dep("q", "1", exclusions=[("*", "z")])]
    assert ("z", "1") not in _set(_resolve(catalog, ("app", "1"), strategy="gradle"))


def test_gradle_takes_a_managed_bom_as_constraints_and_ignores_a_dependency_s_own_management() -> None:
    lib = ("<project><groupId>g</groupId><artifactId>lib</artifactId><version>1</version><dependencyManagement>"
           "<dependencies><dependency><groupId>g</groupId><artifactId>bom</artifactId><version>2</version>"
           "<type>pom</type><scope>import</scope></dependency></dependencies></dependencyManagement></project>")
    catalog = Catalog({"app": _v("1"), "lib": _v("1"), "bom": _v("2"), "c": _v("1.0", "1.5"), "d": _v("1.0", "9.0")},
                      {"app@1": [_dep("lib", "1"), _dep("c", "1.0")]},
                      {"g/lib/1/lib-1.pom": lib, "g/bom/2/bom-2.pom": _pom("bom", "2", _managed("c", "1.5") +
                                                                         _managed("d", "9.0"))})
    # lib's import fills lib's own versions only: no node, no constraint (what Gradle does with POMs).
    assert _set(_resolve(catalog, ("app", "1"), strategy="gradle")) == {("app", "1"), ("lib", "1"), ("c", "1.0")}
    # The project's own BOM (`platform()`): it raises c, and adds no d (nothing depends on d).
    assert _set(_resolve(catalog, ("app", "1"), managed=[("bom", "2", "bom")], strategy="gradle")) == {
        ("app", "1"), ("lib", "1"), ("c", "1.5")}


def test_gradle_managed_entries_raise_but_never_lower_and_add_nothing() -> None:
    catalog = Catalog({"app": _v("1"), "b": _v("1.0", "2.0", "3.0"), "c": _v("1")}, {"app@1": [_dep("b", "2.0")]})
    assert _set(_resolve(catalog, ("app", "1"), managed=[("b", "3.0", None), ("c", "1", None)], strategy="gradle")) \
        == {("app", "1"), ("b", "3.0")}
    assert _set(_resolve(catalog, ("app", "1"), managed=[("b", "1.0", None)], strategy="gradle")) \
        == {("app", "1"), ("b", "2.0")}


def test_gradle_plugins_resolve_through_their_marker_and_gradle_metadata_is_flagged() -> None:
    marker = PackageRef("maven", "org.example.tool", "org.example.tool.gradle.plugin")
    impl = ("<project><groupId>g</groupId><artifactId>impl</artifactId><version>1</version>"
            "<!-- do_not_remove: published-with-gradle-metadata --></project>")
    catalog = Catalog({marker.name: _v("1.2"), "impl": _v("1")}, documents={"g/impl/1/impl-1.pom": impl})
    catalog.deps[f"{marker.name}@1.2"] = [_dep("impl", "1")]
    catalog.data[marker.name] = _v("1.2")

    async def versions(ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return catalog.data.get(ref.name, []) if ref.namespace in ("g", marker.namespace) else []

    catalog.versions = versions  # type: ignore[method-assign]
    request = ResolveRequest(roots=[], profile=PROFILE,
                             environment=parse_environment({"strategy": "gradle", "plugins": "org.example.tool:1.2"}))
    graph = asyncio.run(RESOLVER.resolve(request, catalog))
    assert {(n.ref.name, n.version) for n in graph.nodes} == {(marker.name, "1.2"), ("impl", "1")}
    assert [w["code"] for w in graph.warnings] == ["gradle_module_metadata"]
    assert graph.warnings[0]["package"] == "g:impl"


def test_a_versionless_root_takes_its_managed_version_and_explicit_entries_beat_boms() -> None:
    catalog = Catalog({"app": _v("1.0", "2.0"), "b": _v("1.0", "2.0", "3.0")}, {"app@1.0": [_dep("b", "1.0")]},
                      {"g/bom/5/bom-5.pom": _pom("bom", "5", _managed("app", "1.0") + _managed("b", "2.0"))})
    # The BOM gives app (a versionless project dependency) its 1.0; b listed after the BOM still wins with 3.0.
    graph = _resolve(catalog, ("app", "*"), managed=[("bom", "5", "bom"), ("b", "3.0", None)])
    assert _set(graph) == {("app", "1.0"), ("b", "3.0")}
    assert _set(_resolve(catalog, ("app", "*"))) == {("app", "2.0")}                     # not managed: the highest
    assert _set(_resolve(catalog, ("app", "2.0"), managed=[("app", "1.0", None)])) == {("app", "2.0")}
    gradle = _resolve(catalog, ("app", "*"), managed=[("bom", "5", "bom"), ("b", "3.0", None)], strategy="gradle")
    assert _set(gradle) == {("app", "1.0"), ("b", "3.0")}


def test_maven_puts_a_relocation_target_in_place_and_gradle_follows_it() -> None:
    catalog = Catalog({"app": _v("1"), "old": _v("8"), "new": _v("8"), "proto": _v("3")},
                      {"app@1": [_dep("old", "8")], "old@8": [_dep("new", "8", relocation=True)],
                       "new@8": [_dep("proto", "3")]})
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1"), ("new", "8"), ("proto", "3")}
    nodes = {n.ref.name: n for n in graph.nodes}
    assert nodes["new"].depth == 2 and ("n1", nodes["new"].id) in {(e.source, e.target) for e in graph.edges}
    root = _resolve(catalog, ("old", "8"))                                   # a relocated root: its target is the root
    assert _set(root) == {("new", "8"), ("proto", "3")} and root.nodes[0].latest_compatible == "8"
    assert _set(_resolve(catalog, ("app", "1"), strategy="gradle")) == {
        ("app", "1"), ("old", "8"), ("new", "8"), ("proto", "3")}


def test_a_managed_scope_is_kept_as_set() -> None:
    catalog = Catalog({n: _v("1") for n in ("app", "r", "c")},
                      {"app@1": [_dep("r", "1", "runtime")], "r@1": [_dep("c", "1")]})
    graph = _resolve(catalog, ("app", "1"), managed=[("c", "1", "compile")])
    assert _scopes(graph)["c"] == "compile"                                   # derived it would be runtime
    assert _scopes(_resolve(catalog, ("app", "1")))["c"] == "runtime"


def test_gradle_keeps_a_module_some_path_does_not_exclude_whatever_the_pattern() -> None:
    catalog = Catalog({n: _v("1") for n in ("app", "p", "q", "z")},
                      {"app@1": [_dep("p", "1", exclusions=[("g", "z")]), _dep("q", "1", exclusions=[("*", "z")])],
                       "p@1": [_dep("z", "1")], "q@1": [_dep("z", "1")]})
    assert ("z", "1") not in _set(_resolve(catalog, ("app", "1"), strategy="gradle"))
    catalog.deps["app@1"] = [_dep("p", "1", exclusions=[("g", "z")]), _dep("q", "1", exclusions=[("*", "other")])]
    assert ("z", "1") in _set(_resolve(catalog, ("app", "1"), strategy="gradle"))


def test_gradle_lets_a_fixed_version_inside_a_range_satisfy_it() -> None:
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "b": _v("1.0", "1.5", "1.9", "2.0")},
                      {"app@1": [_dep("b", "1.5"), _dep("x", "1")], "x@1": [_dep("b", "[1.0,2.0)")]})
    assert ("b", "1.5") in _set(_resolve(catalog, ("app", "1"), strategy="gradle"))
    catalog.deps["x@1"] = [_dep("b", "[1.6,2.0)")]                            # 1.5 is outside: the range's highest
    assert ("b", "1.9") in _set(_resolve(catalog, ("app", "1"), strategy="gradle"))


def test_jdk_profiles_follow_the_java_version() -> None:
    catalog = Catalog({n: _v("1") for n in ("app", "jaxb", "legacy")},
                      {"app@1": [_dep("jaxb", "1", markers={"jdk": "[9,)"}),
                                 _dep("legacy", "1", markers={"unless_jdk": ["[9,)"]})]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("jaxb", "1")}          # java 21 by default
    assert _set(_resolve(catalog, ("app", "1"), env={"java_version": "1.8"})) == {("app", "1"), ("legacy", "1")}
    assert jdk_activates("1.8", "1.8.0_392") and jdk_activates("!1.8", "21") and not jdk_activates("[1.6,1.9)", "21")


def test_a_range_anywhere_below_the_winners_is_a_hard_constraint() -> None:
    """Regression (#29): Maven's NearestVersionSelector backtracks from a nearer soft version a range rejects."""
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "b": _v("1.0", "2.0", "2.5", "3.0"), "d": _v("1")},
                      {"app@1": [_dep("b", "1.0"), _dep("x", "1")], "x@1": [_dep("b", "[2.0,3.0)")],
                       "b@1.0": [_dep("d", "1")]})
    graph = _resolve(catalog, ("app", "1"))
    # The nearest acceptable occurrence is the range: its highest version; what only b 1.0 asked for is gone.
    assert _set(graph) == {("app", "1"), ("x", "1"), ("b", "2.5")}
    assert {n.ref.name: n.depth for n in graph.nodes}["b"] == 3
    # A soft version the range accepts stays the winner.
    catalog.deps["app@1"] = [_dep("b", "2.0"), _dep("x", "1")]
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("x", "1"), ("b", "2.0")}


def test_a_range_takes_the_highest_version_every_range_accepts_and_a_managed_version_replaces_it() -> None:
    catalog = Catalog({"app": _v("1"), "x": _v("1"), "y": _v("1"), "b": _v("1.0", "2.0", "2.4", "2.9")},
                      {"app@1": [_dep("x", "1"), _dep("y", "1")], "x@1": [_dep("b", "[1.0,3.0)")],
                       "y@1": [_dep("b", "[2.0,2.5]")]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("x", "1"), ("y", "1"), ("b", "2.4")}
    # dependencyManagement sets the version of transitive occurrences: no range is left to reject it.
    assert ("b", "1.0") in _set(_resolve(catalog, ("app", "1"), managed=[("b", "1.0", None)]))


def test_ranges_of_a_losing_version_do_not_count_and_disjoint_ranges_fail_the_resolve() -> None:
    catalog = Catalog({"app": _v("1"), "x": _v("1", "2"), "y": _v("1"), "b": _v("1.0", "2.0")},
                      {"app@1": [_dep("x", "1"), _dep("b", "1.0"), _dep("y", "1")], "y@1": [_dep("x", "2")],
                       "x@2": [_dep("b", "[2.0,)")]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1"), ("x", "1"), ("b", "1.0"), ("y", "1")}
    catalog.deps = {"app@1": [_dep("x", "1"), _dep("y", "1")], "x@1": [_dep("b", "[1.0,2.0)")],
                    "y@1": [_dep("b", "[2.0,3.0)")]}
    graph = _resolve(catalog, ("app", "1"))
    assert graph.error is not None and graph.error["code"] == "resolver_conflict"
    # As Maven 3.9 reports it: "Could not resolve version conflict among [g:x:jar:1 -> g:b:jar:[1.0,2.0), ...]".
    assert graph.error["message"] == "could not resolve version conflict among [g:x:1 -> g:b:[1.0,2.0), " \
                                     "g:y:1 -> g:b:[2.0,3.0)]"
