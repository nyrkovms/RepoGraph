"""The Go resolver (IMPL H5, SPEC §9.5, Q23) over in-memory go.mod documents."""

import asyncio
from collections.abc import Sequence

from app.ecosystems.base import (
    CatalogVersion,
    FetchedDocument,
    NormalizedDependency,
    PackageRef,
    RegistryConfig,
    ResolveGraph,
    ResolveRequest,
    ResolveRoot,
    SourceProfile,
)
from app.ecosystems.go.environment import parse_environment
from app.ecosystems.go.resolver import NO_GO_MOD, RESOLVER

REGISTRY = RegistryConfig(id="r", slug="proxy-golang-org", ecosystem="go", protocol="goproxy",
                          base_url="https://proxy.example/", priority=0, config={}, routing=None, auth_type="none",
                          credentials_ref=None)
PROFILE = SourceProfile(ecosystem="go", slug="default", registries=[REGISTRY])


def _mod(path: str, go: str | None, *requires: str) -> str:
    lines = [f"module {path}", ""]
    if go:
        lines.append(f"go {go}")
    lines += [f"require {r}" for r in requires]
    return "\n".join(lines) + "\n"


class Catalog:
    """go.mod files by `path@version`; versions lists by path."""

    def __init__(self, mods: dict[str, str], versions: dict[str, list[str]] | None = None) -> None:
        self.mods = mods
        self.lists = versions or {}
        self.read: list[str] = []

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return [CatalogVersion(version=v, registry_id="r", registry_slug="proxy-golang-org", status="active")
                for v in self.lists.get(ref.name, [])]

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        return None

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        return False

    async def document(self, ref: PackageRef, path: str, profile: SourceProfile) -> FetchedDocument | None:
        module, _, rest = path.partition("/@v/")
        key = f"{module}@{rest.removesuffix('.mod')}"
        self.read.append(key)
        text = self.mods.get(key)
        return FetchedDocument(path=path, content=text.encode(), content_type="text/plain") if text else None


def _resolve(catalog: Catalog, *roots: tuple[str, str], env: dict | None = None, **options: object) -> ResolveGraph:
    request = ResolveRequest(roots=[ResolveRoot(ref=PackageRef("go", "", p), constraint=c) for p, c in roots],
                             environment=parse_environment(env or {}), profile=PROFILE, **options)  # type: ignore[arg-type]
    return asyncio.run(RESOLVER.resolve(request, catalog))


def _set(graph: ResolveGraph) -> set[tuple[str, str]]:
    assert graph.error is None, graph.error
    return {(n.ref.name, n.version) for n in graph.nodes}


def test_a_pruned_root_contributes_its_requirements_without_reading_their_go_mod() -> None:
    catalog = Catalog({
        "example.com/app@v1.0.0": _mod("example.com/app", "1.21", "example.com/a v1.2.0", "example.com/b v1.0.0 // indirect"),
        "example.com/a@v1.2.0": _mod("example.com/a", "1.21", "example.com/c v9.0.0"),
    })
    graph = _resolve(catalog, ("example.com/app", "v1.0.0"))
    assert _set(graph) == {("example.com/app", "v1.0.0"), ("example.com/a", "v1.2.0"), ("example.com/b", "v1.0.0")}
    assert catalog.read == ["example.com/app@v1.0.0"]
    ids = {n.id: n.ref.name for n in graph.nodes}
    assert sorted((e.source if e.source == "root" else ids[e.source], ids[e.target], e.details.get("indirect", False))
                  for e in graph.edges) == [("example.com/app", "example.com/a", False),
                                            ("example.com/app", "example.com/b", True), ("root", "example.com/app", False)]
    assert graph.strategy == "go-mvs" and RESOLVER.emulates.startswith("Go 1.23")


def test_below_an_old_module_every_go_mod_is_read_and_mvs_takes_the_highest() -> None:
    # app is on go 1.15: unpruned, so b's go.mod is read even though b says go 1.21, and c rises to v1.3.0.
    catalog = Catalog({
        "example.com/app@v1.0.0": _mod("example.com/app", "1.15", "example.com/b v1.0.0", "example.com/c v1.1.0"),
        "example.com/b@v1.0.0": _mod("example.com/b", "1.21", "example.com/c v1.3.0", "example.com/d v0.1.0"),
        "example.com/c@v1.1.0": _mod("example.com/c", None),
        "example.com/c@v1.3.0": _mod("example.com/c", None, "example.com/e v2.0.0+incompatible"),
        "example.com/d@v0.1.0": _mod("example.com/d", "1.21"),
        "example.com/e@v2.0.0+incompatible": _mod("example.com/e", None),
    })
    assert _set(_resolve(catalog, ("example.com/app", "v1.0.0"))) == {
        ("example.com/app", "v1.0.0"), ("example.com/b", "v1.0.0"), ("example.com/c", "v1.3.0"),
        ("example.com/d", "v0.1.0"), ("example.com/e", "v2.0.0+incompatible")}


def test_requirements_of_versions_mvs_does_not_select_still_count() -> None:
    # c v1.1.0 is read (app requires it) although v1.3.0 is selected: its requirement of d v1.5.0 counts.
    catalog = Catalog({
        "example.com/app@v1.0.0": _mod("example.com/app", None, "example.com/b v1.0.0", "example.com/c v1.1.0"),
        "example.com/b@v1.0.0": _mod("example.com/b", None, "example.com/c v1.3.0"),
        "example.com/c@v1.1.0": _mod("example.com/c", None, "example.com/d v1.5.0"),
        "example.com/c@v1.3.0": _mod("example.com/c", None, "example.com/d v1.0.0"),
        "example.com/d@v1.5.0": _mod("example.com/d", None),
        "example.com/d@v1.0.0": _mod("example.com/d", None),
    })
    assert ("example.com/d", "v1.5.0") in _set(_resolve(catalog, ("example.com/app", "v1.0.0")))


def test_replace_and_exclude_of_the_main_module() -> None:
    catalog = Catalog({
        "example.com/app@v1.0.0": _mod("example.com/app", None, "example.com/a v1.0.0", "example.com/x v1.1.0"),
        "example.com/fork@v1.0.1": _mod("example.com/a", None, "example.com/b v1.0.0"),
        "example.com/b@v1.0.0": _mod("example.com/b", None),
        "example.com/x@v1.2.0": _mod("example.com/x", None),
    }, versions={"example.com/x": ["v1.1.0", "v1.2.0", "v1.3.0-rc.1"]})
    graph = _resolve(catalog, ("example.com/app", "v1.0.0"), env={
        "replace": [{"old": "example.com/a", "new": "example.com/fork@v1.0.1"}], "exclude": ["example.com/x@v1.1.0"]})
    # Since Go 1.16 a requirement on an excluded version is dropped, not moved to the next listed version.
    assert _set(graph) == {("example.com/app", "v1.0.0"), ("example.com/a", "v1.0.0"), ("example.com/b", "v1.0.0")}
    a = next(n for n in graph.nodes if n.ref.name == "example.com/a")
    assert a.details == {"replaced_by": "example.com/fork@v1.0.1"}


def test_root_version_go_mod_problems_and_the_go_version() -> None:
    catalog = Catalog({"example.com/new@v2.1.0": _mod("example.com/new", "1.25"),
                       "example.com/old@v1.0.0": "this is not a go.mod\n"},
                      versions={"example.com/new": ["v1.0.0", "v2.1.0", "v2.2.0-rc.1"],
                                "example.com/old": ["v1.0.0"]})
    graph = _resolve(catalog, ("example.com/new", "*"), ("example.com/old", ""), ("example.com/none", "v1.0.0"))
    assert _set(graph) == {("example.com/new", "v2.1.0"), ("example.com/old", "v1.0.0"), ("example.com/none", "v1.0.0")}
    by_name = {n.ref.name: n for n in graph.nodes}
    assert by_name["example.com/none"].errors == [NO_GO_MOD]
    assert by_name["example.com/old"].errors[0].startswith("go.mod cannot be read")
    assert by_name["example.com/new"].latest_compatible == "v2.1.0"
    assert [w["code"] for w in graph.warnings] == ["go_version"]   # needs go 1.25, the environment has 1.23
    assert _resolve(Catalog({}), ("example.com/ghost", "*")).error["code"] == "not_found"  # type: ignore[index]


def test_limits() -> None:
    catalog = Catalog({"example.com/app@v1.0.0": _mod("example.com/app", None, "example.com/a v1.0.0", "example.com/b v1.0.0")})
    assert _resolve(catalog, ("example.com/app", "v1.0.0"), max_lazy_fetches=1).error["code"] == "resolve_limit_exceeded"  # type: ignore[index]
    assert _resolve(catalog, ("example.com/app", "v1.0.0"), max_nodes=2).error["code"] == "resolve_limit_exceeded"  # type: ignore[index]


def test_environment_parsing() -> None:
    env = parse_environment({"go_version": "go1.22", "exclude": "example.com/x@v1.0.0",
                             "replace": [{"old": "example.com/a@v1.0.0", "new": "example.com/b@v1.1.0"}]})
    assert (env.go_version, env.exclude) == ("1.22", (("example.com/x", "v1.0.0"),))
    assert env.replacement("example.com/a", "v1.0.0") == ("example.com/b", "v1.1.0")
    assert env.replacement("example.com/a", "v1.0.1") is None
    for bad in ({"exclude": ["example.com/x"]}, {"replace": [{"old": "a", "new": "../local"}]}, {"replace": ["x"]}):
        try:
            parse_environment(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should be refused")
    assert parse_environment({}).specified is False


def test_a_root_raised_by_mvs_is_read_again_from_the_selected_version() -> None:
    # Go's expandGraph: z needs x v1.1.0, whose go.mod brings w; reading only x v1.0.0 would miss w.
    catalog = Catalog({
        "example.com/x@v1.0.0": _mod("example.com/x", "1.21"),
        "example.com/x@v1.1.0": _mod("example.com/x", "1.21", "example.com/w v1.0.0"),
        "example.com/z@v1.0.0": _mod("example.com/z", "1.21", "example.com/x v1.1.0"),
    })
    assert _set(_resolve(catalog, ("example.com/x", "v1.0.0"), ("example.com/z", "v1.0.0"))) == {
        ("example.com/x", "v1.1.0"), ("example.com/z", "v1.0.0"), ("example.com/w", "v1.0.0")}


def test_a_go_mod_declaring_another_path_is_refused_and_the_go_line_is_compared_by_patch() -> None:
    catalog = Catalog({"example.com/app@v1.0.0": _mod("example.com/app", "1.23.0", "example.com/moved v1.0.0"),
                       "example.com/moved@v1.0.0": _mod("example.com/elsewhere", "1.21", "example.com/q v1.0.0")})
    graph = _resolve(catalog, ("example.com/moved", "v1.0.0"), ("example.com/app", "v1.0.0"))
    moved = next(n for n in graph.nodes if n.ref.name == "example.com/moved")
    assert moved.errors == ["go.mod declares its path as example.com/elsewhere"]
    assert ("example.com/q", "v1.0.0") not in _set(graph)
    assert graph.warnings == []   # go 1.23.0 needs nothing beyond the environment's 1.23


def test_the_latest_tag_is_the_root_for_star_and_the_latest_compatible() -> None:
    catalog = Catalog({"example.com/m@v1.5.0": _mod("example.com/m", "1.21")}, versions={})
    tagged = [CatalogVersion(version=v, registry_id="r", registry_slug="proxy-golang-org", status="active", tags=t)
              for v, t in (("v1.5.0", ("latest",)), ("v2.0.0+incompatible", ()))]

    async def versions(ref, profile):
        return tagged

    catalog.versions = versions  # type: ignore[method-assign]
    graph = _resolve(catalog, ("example.com/m", "*"))
    assert _set(graph) == {("example.com/m", "v1.5.0")} and graph.nodes[0].latest_compatible == "v1.5.0"
