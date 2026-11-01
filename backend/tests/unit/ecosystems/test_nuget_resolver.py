"""The NuGet resolver (IMPL I5, SPEC §9.6) over an in-memory catalog."""

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
from app.ecosystems.nuget.environment import parse_environment
from app.ecosystems.nuget.resolver import RESOLVER, UNKNOWN_DEPENDENCIES

PROFILE = SourceProfile(ecosystem="nuget", slug="default", registries=[])


def _ref(name: str) -> PackageRef:
    return PackageRef("nuget", "", name.lower())


def _v(version: str, status: str = "active") -> CatalogVersion:
    return CatalogVersion(version=version, registry_id="r", registry_slug="nuget-org", status=status)  # type: ignore[arg-type]


def _deps(*items: tuple[str, str], group: str = ".NETStandard2.0", groups: list[str] | None = None) -> list[NormalizedDependency]:
    return [NormalizedDependency(target=_ref(name), constraint=rng, dependency_type="runtime", raw_spec=f"{name} {rng}",
                                 environment_markers={"dotnet_target_framework": group},
                                 details={"groups": groups or [group]}) for name, rng in items]


class Catalog:
    def __init__(self, versions: dict[str, list[CatalogVersion]],
                 deps: dict[str, list[NormalizedDependency] | None] | None = None) -> None:
        self.data = {name.lower(): v for name, v in versions.items()}
        self.deps = deps or {}

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return self.data.get(ref.name, [])

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        return self.deps.get(f"{ref.name}@{version}", [])

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        return False

    async def document(self, ref: PackageRef, path: str, profile: SourceProfile) -> FetchedDocument | None:
        return None


def _resolve(catalog: Catalog, *roots: tuple[str, str], env: dict | None = None) -> ResolveGraph:
    request = ResolveRequest(roots=[ResolveRoot(ref=_ref(n), constraint=c) for n, c in roots],
                             environment=parse_environment(env or {}), profile=PROFILE)
    return asyncio.run(RESOLVER.resolve(request, catalog))


def _set(graph: ResolveGraph) -> set[tuple[str, str]]:
    assert graph.error is None, graph.error
    return {(n.ref.name, n.version) for n in graph.nodes}


def test_lowest_applicable_version_and_floating() -> None:
    catalog = Catalog({"a": [_v("1.0.0"), _v("1.2.0"), _v("1.5.0"), _v("2.0.0-beta")], "b": [_v("3.0.0"), _v("3.1.0")]},
                      {"a@1.2.0": _deps(("b", "[3.0.0, )"))})
    assert _set(_resolve(catalog, ("a", "1.1"))) == {("a", "1.2.0"), ("b", "3.0.0")}
    assert _set(_resolve(catalog, ("a", "1.*"))) == {("a", "1.5.0")}
    assert _resolve(catalog, ("a", "1.1")).strategy == "nuget-nearest-wins" and RESOLVER.emulates.startswith(".NET SDK 8")


def test_a_direct_declaration_eclipses_its_subtree_and_a_downgrade_fails_the_restore() -> None:
    catalog = Catalog({"app": [_v("1.0.0")], "lib": [_v("1.0.0")], "core": [_v("1.0.0"), _v("2.0.0")]},
                      {"app@1.0.0": _deps(("lib", "1.0.0"), ("core", "1.0.0")), "lib@1.0.0": _deps(("core", "2.0.0"))})
    graph = _resolve(catalog, ("app", "1.0.0"))
    # NuGet writes the assets file of the failed restore: the graph stays, the error says why.
    assert {(n.ref.name, n.version) for n in graph.nodes} == {("app", "1.0.0"), ("lib", "1.0.0"), ("core", "1.0.0")}
    assert graph.error is not None and graph.error["code"] == "NU1605"
    catalog.deps["lib@1.0.0"] = _deps(("core", "[0.5.0, )"))     # wanting less is no downgrade
    assert _set(_resolve(catalog, ("app", "1.0.0"))) == {("app", "1.0.0"), ("lib", "1.0.0"), ("core", "1.0.0")}


def test_cousins_at_any_depth_take_the_highest_best_match_or_conflict() -> None:
    # a -> c [1.0.0, ) at depth 2; b -> d -> c [2.0.0, ) at depth 3: cousins, the highest wins (not the nearer).
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")], "d": [_v("1.0.0")], "c": [_v("1.0.0"), _v("2.0.0")]},
                      {"a@1.0.0": _deps(("c", "[1.0.0, )")), "b@1.0.0": _deps(("d", "1.0.0")),
                       "d@1.0.0": _deps(("c", "[2.0.0, )"))})
    graph = _resolve(catalog, ("a", "1.0.0"), ("b", "1.0.0"))
    assert ("c", "2.0.0") in _set(graph) and graph.warnings == []
    catalog.deps["a@1.0.0"] = _deps(("c", "[1.0.0]"))           # the winner breaks a's exact range
    graph = _resolve(catalog, ("a", "1.0.0"), ("b", "1.0.0"))
    assert graph.error is not None and graph.error["code"] == "NU1107" and "c" in graph.error["message"]


def test_cousins_skip_prereleases_each_range_would_not_take() -> None:
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")], "c": [_v("1.0.0"), _v("1.2.0-beta"), _v("1.2.0")]},
                      {"a@1.0.0": _deps(("c", "[1.0.0, )")), "b@1.0.0": _deps(("c", "[1.1.0, )"))})
    assert ("c", "1.2.0") in _set(_resolve(catalog, ("a", "1.0.0"), ("b", "1.0.0")))


def test_the_versions_that_lose_take_their_subtrees_with_them() -> None:
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")], "c": [_v("1.0.0"), _v("2.0.0")], "old": [_v("1.0.0")]},
                      {"a@1.0.0": _deps(("c", "1.0.0")), "b@1.0.0": _deps(("c", "2.0.0")),
                       "c@1.0.0": _deps(("old", "1.0.0"))})
    assert _set(_resolve(catalog, ("a", "1.0.0"), ("b", "1.0.0"))) == {
        ("a", "1.0.0"), ("b", "1.0.0"), ("c", "2.0.0")}


def test_a_cycle_fails_the_restore() -> None:
    catalog = Catalog({"p": [_v("1.0.0")], "q": [_v("1.0.0")]},
                      {"p@1.0.0": _deps(("q", "1.0.0")), "q@1.0.0": _deps(("p", "1.0.0"))})
    graph = _resolve(catalog, ("p", "1.0.0"))
    assert graph.error is not None and graph.error["code"] == "NU1108"


def test_the_group_of_the_target_framework_decides_the_dependencies() -> None:
    groups = ["net8.0", ".NETStandard2.0", "net10.0"]
    deps = (_deps(("only-standard", "1.0.0"), group=".NETStandard2.0", groups=groups)
            + _deps(("only-net8", "1.0.0"), group="net8.0", groups=groups)
            + _deps(("only-net10", "1.0.0"), group="net10.0", groups=groups))
    catalog = Catalog({"a": [_v("1.0.0")], "only-standard": [_v("1.0.0")], "only-net8": [_v("1.0.0")],
                       "only-net10": [_v("1.0.0")]}, {"a@1.0.0": deps})
    assert _set(_resolve(catalog, ("a", "1.0.0"))) == {("a", "1.0.0"), ("only-net8", "1.0.0")}
    assert ("only-net10", "1.0.0") in _set(_resolve(catalog, ("a", "1.0.0"), env={"target_framework": "net10.0"}))
    assert ("only-standard", "1.0.0") in _set(_resolve(catalog, ("a", "1.0.0"), env={"target_framework": "net5.0"}))


def test_unlisted_versions_count_like_any_other() -> None:
    # Restore reads the full version list: Microsoft.Azure.KeyVault >= 2.3.0 takes the unlisted 2.3.1 (reference case).
    catalog = Catalog({"a": [_v("1.0.0", "unlisted"), _v("1.1.0")]})
    assert _set(_resolve(catalog, ("a", "1.0.0"))) == {("a", "1.0.0")}
    assert _set(_resolve(catalog, ("a", "1.*"))) == {("a", "1.1.0")}


def test_a_version_that_lost_does_not_bring_its_dependencies() -> None:
    catalog = Catalog({"app": [_v("1.0.0")], "lib": [_v("1.0.0")], "x": [_v("1.0.0"), _v("2.0.0")], "y": [_v("1.0.0")]},
                      {"app@1.0.0": _deps(("lib", "1.0.0"), ("x", "2.0.0")), "lib@1.0.0": _deps(("x", "1.0.0")),
                       "x@1.0.0": _deps(("y", "1.0.0"))})
    assert _set(_resolve(catalog, ("app", "1.0.0"))) == {("app", "1.0.0"), ("lib", "1.0.0"), ("x", "2.0.0")}


def test_unknown_dependencies_missing_packages_and_bad_ranges() -> None:
    catalog = Catalog({"a": [_v("1.0.0")]}, {"a@1.0.0": None})
    graph = _resolve(catalog, ("a", "1.0.0"))
    assert graph.nodes[0].errors == [UNKNOWN_DEPENDENCIES]
    assert _resolve(Catalog({}), ("ghost", "1.0.0")).error["code"] == "not_found"  # type: ignore[index]
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")]}, {"a@1.0.0": _deps(("b", "[1.0"), ("gone", "1.0.0"))})
    graph = _resolve(catalog, ("a", "1.0.0"))
    a = next(n for n in graph.nodes if n.ref.name == "a")
    assert a.errors == ["b '[1.0' is not a NuGet version range", "no version of gone matches 1.0.0"]


def test_a_version_with_only_framework_groups_is_reported() -> None:
    deps = _deps(("old", "1.0.0"), group=".NETFramework4.6.2", groups=[".NETFramework4.6.2"])
    catalog = Catalog({"a": [_v("1.0.0")], "old": [_v("1.0.0")]}, {"a@1.0.0": deps})
    graph = _resolve(catalog, ("a", "1.0.0"))
    assert _set(graph) == {("a", "1.0.0")} and [w["code"] for w in graph.warnings] == ["no_framework_group"]


def _warnings(graph: ResolveGraph) -> list[tuple[str, str]]:
    return [(w["code"], w["message"]) for w in graph.warnings]


def test_restore_warns_about_lower_bounds_and_ranges_a_direct_reference_exceeds() -> None:
    """Regression (#30): the warnings `dotnet restore` (SDK 8) gives on the same feed, messages as it writes them."""
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0"), _v("2.1.0")], "c": [_v("1.0.1"), _v("1.1.0")],
                       "e": [_v("1.0.0"), _v("1.0.1")], "f": [_v("1.0.0")], "g": [_v("1.0.0")],
                       "h": [_v("1.0.1"), _v("1.2.0")], "p": [_v("1.0.0")], "m": [_v("1.0.0")],
                       "n": [_v("1.5.0"), _v("2.5.0")], "x": [_v("1.0.0")], "y": [_v("1.0.0")]},
                      {"a@1.0.0": _deps(("b", "[1.0.0, 2.0.0)")), "f@1.0.0": _deps(("h", "1.0.0")),
                       "g@1.0.0": _deps(("h", "1.2.0")), "p@1.0.0": _deps(("m", "1.0.0")),
                       "m@1.0.0": _deps(("n", "[1.0.0, 2.0.0)")), "x@1.0.0": _deps(("e", "(1.0.0, )")),
                       "y@1.0.0": _deps(("e", ""))})
    assert _warnings(_resolve(catalog, ("a", "1.0.0"), ("b", "2.1.0"))) == [(
        "NU1608", "Detected package version outside of dependency constraint: a 1.0.0 requires b (>= 1.0.0 && < 2.0.0) "
                  "but version b 2.1.0 was resolved.")]
    assert _warnings(_resolve(catalog, ("p", "1.0.0"), ("n", "2.5.0")))[0][0] == "NU1608"   # two levels down
    assert _warnings(_resolve(catalog, ("c", "1.0.0"))) == [(
        "NU1603", "the project depends on c (>= 1.0.0) but c 1.0.0 was not found. An approximate best match of c 1.0.1 "
                  "was resolved.")]
    assert _warnings(_resolve(catalog, ("c", "1.0.*"))) == []                                  # floating
    # The edge counts as walked, though h 1.0.1 loses to its cousin 1.2.0.
    graph = _resolve(catalog, ("f", "1.0.0"), ("g", "1.0.0"))
    assert ("h", "1.2.0") in _set(graph) and _warnings(graph) == [(
        "NU1603", "f 1.0.0 depends on h (>= 1.0.0) but h 1.0.0 was not found. An approximate best match of h 1.0.1 "
                  "was resolved.")]
    assert _warnings(_resolve(catalog, ("e", "(1.0.0, )"))) == [(
        "NU1604", "Project dependency e (> 1.0.0) does not contain an inclusive lower bound. Include a lower bound in "
                  "the dependency version to ensure consistent restore results.")]
    assert _warnings(_resolve(catalog, ("x", "1.0.0"))) == [(
        "NU1602", "x 1.0.0 does not provide an inclusive lower bound for dependency e (> 1.0.0). An approximate best "
                  "match of e 1.0.1 was resolved.")]
    assert _warnings(_resolve(catalog, ("y", "1.0.0"))) == [(
        "NU1602", "y 1.0.0 does not provide an inclusive lower bound for dependency e. An approximate best match of "
                  "e 1.0.0 was resolved.")]


def test_an_eclipsed_edge_gives_no_lower_bound_warning_and_an_exact_range_prints_as_equal() -> None:
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.1"), _v("2.0.0")], "u": [_v("1.0.0")],
                       "v": [_v("1.0.0"), _v("2.0.0")], "q": [_v("1.0.0")], "r": [_v("1.0.0")],
                       "s": [_v("1.0.0"), _v("2.0.0")], "t": [_v("1.0.1")]},
                      {"a@1.0.0": _deps(("b", "1.0.0")), "u@1.0.0": _deps(("v", "[1.0.0]")),
                       "q@1.0.0": _deps(("s", "1.0.0")), "r@1.0.0": _deps(("s", "2.0.0")),
                       "s@1.0.0": _deps(("t", "1.0.0"))})
    assert _warnings(_resolve(catalog, ("a", "1.0.0"), ("b", "2.0.0"))) == []
    assert _warnings(_resolve(catalog, ("u", "1.0.0"), ("v", "2.0.0"))) == [(
        "NU1608", "Detected package version outside of dependency constraint: u 1.0.0 requires v (= 1.0.0) but version "
                  "v 2.0.0 was resolved.")]
    # Below a version that later loses (s 1.0.0 to 2.0.0), as restore does.
    assert _warnings(_resolve(catalog, ("q", "1.0.0"), ("r", "1.0.0")))[0][0] == "NU1603"
