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


def test_the_nearest_reference_wins_and_a_downgrade_is_reported() -> None:
    catalog = Catalog({"app": [_v("1.0.0")], "lib": [_v("1.0.0")], "core": [_v("1.0.0"), _v("2.0.0")]},
                      {"app@1.0.0": _deps(("lib", "1.0.0"), ("core", "1.0.0")), "lib@1.0.0": _deps(("core", "2.0.0"))})
    graph = _resolve(catalog, ("app", "1.0.0"))
    assert _set(graph) == {("app", "1.0.0"), ("lib", "1.0.0"), ("core", "1.0.0")}
    assert [w["code"] for w in graph.warnings] == ["NU1605"]


def test_cousins_take_the_lowest_version_all_ranges_allow_or_conflict() -> None:
    catalog = Catalog({"a": [_v("1.0.0")], "b": [_v("1.0.0")], "c": [_v("1.0.0"), _v("1.5.0"), _v("2.0.0")]},
                      {"a@1.0.0": _deps(("c", "1.0.0")), "b@1.0.0": _deps(("c", "[1.5.0, 3.0.0)"))})
    assert _set(_resolve(catalog, ("a", "1.0.0"), ("b", "1.0.0"))) == {("a", "1.0.0"), ("b", "1.0.0"), ("c", "1.5.0")}
    catalog.deps["b@1.0.0"] = _deps(("c", "[0.5.0, 1.0.0)"))
    graph = _resolve(catalog, ("a", "1.0.0"), ("b", "1.0.0"))
    assert graph.error is not None and graph.error["code"] == "NU1107" and graph.error["package"] == "c"


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


def test_unlisted_versions_only_when_nothing_listed_fits() -> None:
    catalog = Catalog({"a": [_v("1.0.0", "unlisted"), _v("1.1.0")]})
    assert _set(_resolve(catalog, ("a", "1.0.0"))) == {("a", "1.1.0")}
    assert _set(_resolve(catalog, ("a", "[1.0.0]"))) == {("a", "1.0.0")}


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
    a = next(n for n in _resolve(catalog, ("a", "1.0.0")).nodes if n.ref.name == "a")
    assert a.errors == ["b '[1.0' is not a NuGet version range", "no version of gone matches 1.0.0"]
