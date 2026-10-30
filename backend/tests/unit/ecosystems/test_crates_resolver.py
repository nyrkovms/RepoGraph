"""The crates resolver (IMPL G5, SPEC §9.4) over an in-memory catalog."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest

from app.ecosystems.base import (
    CatalogVersion,
    NormalizedDependency,
    PackageRef,
    ResolveGraph,
    ResolveRequest,
    ResolveRoot,
    SourceProfile,
)
from app.ecosystems.crates.environment import parse_environment
from app.ecosystems.crates.resolver import RESOLVER, UNKNOWN_DEPENDENCIES, enabled

PROFILE = SourceProfile(ecosystem="crates", slug="default", registries=[])
LINUX, WINDOWS = "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"


def _ref(name: str) -> PackageRef:
    return PackageRef("crates", "", name)


def _v(version: str, status: str = "active", **features: list[str]) -> CatalogVersion:
    return CatalogVersion(version=version, registry_id="r", registry_slug="crates-io", status=status,  # type: ignore[arg-type]
                          features=features)


def _dep(name: str, req: str, kind: str = "runtime", *, package: str | None = None, features: list[str] | None = None,
         default_features: bool = True, target: str | None = None) -> NormalizedDependency:
    details: dict[str, object] = {}
    if features:
        details["features"] = features
    if not default_features:
        details["default_features"] = False
    return NormalizedDependency(
        target=_ref(package or name), constraint=req, dependency_type=kind,  # type: ignore[arg-type]
        raw_spec=f'{name} = "{req}"', scope=kind, alias=name if package else None,
        environment_markers={"rust_cfg": target} if target else None, details=details or None)


class Catalog:
    def __init__(self, versions: dict[str, list[CatalogVersion]],
                 deps: dict[str, list[NormalizedDependency] | None] | None = None,
                 fetchable: dict[str, list[CatalogVersion]] | None = None) -> None:
        self.data = {_ref(name): v for name, v in versions.items()}
        self.deps = deps or {}
        self.fetchable = {_ref(name): v for name, v in (fetchable or {}).items()}
        self.ensured: list[str] = []

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        return self.data.get(ref, [])

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        return self.deps.get(f"{ref.name}@{version}", [])

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        self.ensured.append(ref.name)
        if ref in self.fetchable:
            self.data[ref] = self.fetchable.pop(ref)
            return True
        return False


def _resolve(catalog: Catalog, *roots: tuple[str, str] | tuple[str, str, list[str]], env: dict | None = None,
             **options: object) -> ResolveGraph:
    request = ResolveRequest(
        roots=[ResolveRoot(ref=_ref(r[0]), constraint=r[1], extras=r[2] if len(r) > 2 else ()) for r in roots],  # type: ignore[misc]
        environment=parse_environment(env or {}), profile=PROFILE, **options)  # type: ignore[arg-type]
    return asyncio.run(RESOLVER.resolve(request, catalog))


def _set(graph: ResolveGraph) -> set[tuple[str, str]]:
    assert graph.error is None, graph.error
    return {(n.ref.name, n.version) for n in graph.nodes}


# --- versions ----------------------------------------------------------------------------------------------------


def test_newest_matching_version_and_shared_dependencies() -> None:
    catalog = Catalog(
        {"app": [_v("1.0.0")], "a": [_v("1.2.0"), _v("1.3.0"), _v("2.0.0")], "b": [_v("0.4.0")]},
        {"app@1.0.0": [_dep("a", "^1.2"), _dep("b", "0.4")], "b@0.4.0": [_dep("a", "^1")]},
    )
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1.0.0"), ("a", "1.3.0"), ("b", "0.4.0")}
    assert {(n.ref.name, n.depth) for n in graph.nodes} == {("app", 1), ("a", 2), ("b", 2)}
    ids = {n.id: n.ref.name for n in graph.nodes}
    assert sorted((e.source if e.source == "root" else ids[e.source], ids[e.target]) for e in graph.edges) == [
        ("app", "a"), ("app", "b"), ("b", "a"), ("root", "app")]
    assert graph.strategy == "cargo-resolver" and RESOLVER.emulates.startswith("Cargo")


def test_versions_of_different_compatibility_classes_live_side_by_side() -> None:
    catalog = Catalog(
        {"app": [_v("1.0.0")], "x": [_v("1.0.0")], "rand": [_v("0.7.3"), _v("0.8.5"), _v("0.9.0")]},
        {"app@1.0.0": [_dep("rand", "0.8"), _dep("x", "1")], "x@1.0.0": [_dep("rand", "0.7")]},
    )
    assert _set(_resolve(catalog, ("app", "1"))) == {
        ("app", "1.0.0"), ("x", "1.0.0"), ("rand", "0.8.5"), ("rand", "0.7.3")}


def test_one_version_per_class_backtracks_to_one_that_satisfies_everyone() -> None:
    # serde ^1 is tried at 1.0.9 first; the later =1.0.5 forces the class back to 1.0.5.
    catalog = Catalog(
        {"app": [_v("1.0.0")], "pin": [_v("1.0.0")], "serde": [_v("1.0.5"), _v("1.0.9")]},
        {"app@1.0.0": [_dep("serde", "1"), _dep("pin", "1")], "pin@1.0.0": [_dep("serde", "=1.0.5")]},
    )
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1.0.0"), ("pin", "1.0.0"), ("serde", "1.0.5")}


def test_a_wide_requirement_takes_the_newest_class() -> None:
    catalog = Catalog({"app": [_v("1.0.0")], "log": [_v("0.3.9"), _v("0.4.22")]},
                      {"app@1.0.0": [_dep("log", ">=0.3")]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1.0.0"), ("log", "0.4.22")}


def test_yanked_versions_are_never_chosen_and_prereleases_only_when_named() -> None:
    # Cargo 1.86 refuses a yanked version in a new lock even for an exact pin ("version 1.0.95 is yanked").
    catalog = Catalog({"a": [_v("1.0.0"), _v("1.1.0", "yanked"), _v("1.2.0-rc.1")]})
    assert _set(_resolve(catalog, ("a", "1"))) == {("a", "1.0.0")}
    pinned = _resolve(catalog, ("a", "=1.1.0"))
    assert pinned.nodes == [] and pinned.warnings == [{"code": "unresolvable", "message": "no version of a matches =1.1.0"}]
    assert _set(_resolve(catalog, ("a", "^1.2.0-rc.1"))) == {("a", "1.2.0-rc.1")}
    assert _resolve(catalog, ("a", "1")).nodes[0].latest_compatible == "1.0.0"


def test_resolution_impossible_and_nothing_matching() -> None:
    catalog = Catalog(
        {"app": [_v("1.0.0")], "p": [_v("1.0.0")], "q": [_v("1.0.0")], "s": [_v("1.0.1"), _v("1.0.2")]},
        {"app@1.0.0": [_dep("p", "1"), _dep("q", "1"), _dep("gone", "1")], "p@1.0.0": [_dep("s", "=1.0.1")],
         "q@1.0.0": [_dep("s", "=1.0.2")]},
    )
    graph = _resolve(catalog, ("app", "1"))
    assert graph.error is not None and graph.error["code"] == "resolution_impossible"
    assert "s 1.0.1 is already chosen" in graph.error["message"] or "s 1.0.2 is already chosen" in graph.error["message"]
    catalog.deps["q@1.0.0"] = [_dep("s", ">=1.0.1")]
    graph = _resolve(catalog, ("app", "1"))
    assert ("s", "1.0.1") in _set(graph)
    app = next(n for n in graph.nodes if n.ref.name == "app")
    assert app.errors == ["no version of gone matches 1"]
    assert _resolve(Catalog({}), ("nothing", "1")).error["code"] == "not_found"  # type: ignore[index]


def test_lazy_fetch_unknown_dependencies_and_limits() -> None:
    catalog = Catalog({"app": [_v("1.0.0")]}, {"app@1.0.0": [_dep("late", "1")], "late@1.0.0": None},
                      fetchable={"late": [_v("1.0.0")]})
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1.0.0"), ("late", "1.0.0")} and graph.fetched == [_ref("late")]
    late = next(n for n in graph.nodes if n.ref.name == "late")
    assert late.errors == [UNKNOWN_DEPENDENCIES] and graph.warnings[0]["code"] == "dependencies_unknown"
    graph = _resolve(Catalog({"app": [_v("1.0.0")], "x": [_v("1.0.0")]}, {"app@1.0.0": [_dep("x", "1")]}),
                     ("app", "1"), max_nodes=1)
    assert graph.error is not None and graph.error["code"] == "resolve_limit_exceeded"


# --- features ----------------------------------------------------------------------------------------------------


def test_feature_closure() -> None:
    deps = [_dep("serde_derive", "1", "optional"), _dep("std_dep", "1", "optional"), _dep("core", "1"),
            _dep("weak", "1", "optional"), _dep("old", "1", "optional"), _dep("tests", "1", "dev")]
    features = {"default": ["std"], "std": ["dep:std_dep", "core/std", "weak?/std"], "derive": ["serde_derive"],
                "both": ["derive", "old/extra"]}
    on = enabled(features, deps, frozenset({"default"}))
    assert on.entries == {1, 2} and on.missing == set()
    assert on.asks == {"core": {"std"}}                     # weak?/std: weak is off, nothing asked of it
    on = enabled(features, deps, frozenset({"both", "weak"}))
    assert on.entries == {0, 2, 3, 4}                         # implicit features `serde_derive`, `weak`; `old/extra`
    assert on.asks == {"old": {"extra"}}
    on = enabled(features, deps, frozenset({"std", "weak"}))
    assert on.asks == {"core": {"std"}, "weak": {"std"}}       # weak?/std applies once weak is on
    assert enabled(features, deps, frozenset({"std_dep"})).missing == {"std_dep"}   # dep: hides the implicit feature
    assert enabled(features, deps, frozenset({"nope"})).missing == {"nope"}
    assert 5 in enabled(features, deps, frozenset(), include_dev=True).entries
    cyclic = enabled({"a": ["b"], "b": ["a", "dep:std_dep"]}, deps, frozenset({"a"}))
    assert 1 in cyclic.entries


def test_optional_dependencies_follow_unified_features() -> None:
    catalog = Catalog(
        {"app": [_v("1.0.0")], "other": [_v("1.0.0")],
         "serde": [_v("1.0.0", default=["std"], std=[], derive=["dep:serde_derive"])], "serde_derive": [_v("1.0.0")]},
        {"app@1.0.0": [_dep("serde", "1"), _dep("other", "1")],
         "other@1.0.0": [_dep("serde", "1", features=["derive"])],
         "serde@1.0.0": [_dep("serde_derive", "1", "optional")]},
    )
    graph = _resolve(catalog, ("app", "1"))
    assert ("serde_derive", "1.0.0") in _set(graph)          # `other` asked for derive; features are unified
    serde = next(n for n in graph.nodes if n.ref.name == "serde")
    assert serde.details == {"features": ["default", "derive"]}
    catalog.deps["other@1.0.0"] = [_dep("serde", "1")]
    assert ("serde_derive", "1.0.0") not in _set(_resolve(catalog, ("app", "1")))
    assert ("serde_derive", "1.0.0") in _set(_resolve(catalog, ("serde", "1", ["derive"])))


def test_a_version_without_an_asked_feature_is_skipped() -> None:
    catalog = Catalog({"a": [_v("1.0.0", fancy=[]), _v("1.1.0")]})
    assert _set(_resolve(catalog, ("a", "1", ["fancy"]))) == {("a", "1.0.0")}


def test_root_features_and_default_features_from_the_environment() -> None:
    catalog = Catalog({"a": [_v("1.0.0", default=["dep:d"], extra=["dep:e"])], "d": [_v("1.0.0")], "e": [_v("1.0.0")]},
                      {"a@1.0.0": [_dep("d", "1", "optional"), _dep("e", "1", "optional")]})
    assert _set(_resolve(catalog, ("a", "1"))) == {("a", "1.0.0"), ("d", "1.0.0")}
    graph = _resolve(catalog, ("a", "1"), env={"features": ["extra"], "default_features": False})
    assert _set(graph) == {("a", "1.0.0"), ("e", "1.0.0")}


def test_renamed_dependency_takes_features_by_its_local_name() -> None:
    catalog = Catalog(
        {"app": [_v("1.0.0", tls=["native-tls-crate/alpn"])], "native-tls": [_v("0.2.0", alpn=[])]},
        {"app@1.0.0": [_dep("native-tls-crate", "0.2", "optional", package="native-tls")]},
    )
    graph = _resolve(catalog, ("app", "1", ["tls"]))
    assert ("native-tls", "0.2.0") in _set(graph)
    assert next(n for n in graph.nodes if n.ref.name == "native-tls").details == {"features": ["alpn", "default"]}


# --- platforms -----------------------------------------------------------------------------------------------------


@pytest.fixture
def platform_catalog() -> Catalog:
    # winapi only on windows; on windows it also turns on `extra` of shared, which brings `extra-dep`.
    return Catalog(
        {"app": [_v("1.0.0")], "winapi": [_v("0.3.9")], "libc": [_v("0.2.150")],
         "shared": [_v("1.0.0", extra=["dep:extra-dep"])], "extra-dep": [_v("1.0.0")]},
        {"app@1.0.0": [_dep("winapi", "0.3", target="cfg(windows)"), _dep("libc", "0.2", target="cfg(unix)"),
                       _dep("shared", "1")],
         "winapi@0.3.9": [_dep("shared", "1", features=["extra"])],
         "shared@1.0.0": [_dep("extra-dep", "1", "optional")]},
    )


def test_versions_are_chosen_for_every_platform_and_the_graph_is_for_the_target(platform_catalog: Catalog) -> None:
    everything = {("app", "1.0.0"), ("winapi", "0.3.9"), ("libc", "0.2.150"), ("shared", "1.0.0"),
                  ("extra-dep", "1.0.0")}
    assert _set(_resolve(platform_catalog, ("app", "1"), env={"target": "*"})) == everything
    assert _set(_resolve(platform_catalog, ("app", "1"), env={"target": WINDOWS})) == everything - {("libc", "0.2.150")}
    linux = _resolve(platform_catalog, ("app", "1"), env={"target": LINUX})
    # Features winapi asks for on windows do not count on linux, so extra-dep is not built there.
    assert _set(linux) == {("app", "1.0.0"), ("libc", "0.2.150"), ("shared", "1.0.0")}
    assert {e.details.get("rust_cfg") for e in linux.edges} == {None, "cfg(unix)"}


def test_dev_dependencies_only_of_the_roots_and_on_request() -> None:
    catalog = Catalog({"app": [_v("1.0.0")], "lib": [_v("1.0.0")], "t": [_v("1.0.0")], "u": [_v("1.0.0")]},
                      {"app@1.0.0": [_dep("t", "1", "dev"), _dep("lib", "1")], "lib@1.0.0": [_dep("u", "1", "dev")]})
    assert _set(_resolve(catalog, ("app", "1"))) == {("app", "1.0.0"), ("lib", "1.0.0")}
    assert _set(_resolve(catalog, ("app", "1"), include_dev=True)) == {("app", "1.0.0"), ("lib", "1.0.0"), ("t", "1.0.0")}


def test_weak_features_bring_their_dependency_into_the_lock_but_not_the_build() -> None:
    # reqwest's `quinn?/ring`: Cargo.lock holds quinn (rust-lang/cargo#10801), cargo tree does not build it.
    catalog = Catalog(
        {"app": [_v("1.0.0", tls=["quinn?/ring"])], "quinn": [_v("0.11.0", ring=[])]},
        {"app@1.0.0": [_dep("quinn", "0.11", "optional")]},
    )
    assert _set(_resolve(catalog, ("app", "1", ["tls"]))) == {("app", "1.0.0")}
    lock = _resolve(catalog, ("app", "1", ["tls"]), env={"target": "*"})
    assert _set(lock) == {("app", "1.0.0"), ("quinn", "0.11.0")}
    assert next(n for n in lock.nodes if n.ref.name == "quinn").details == {"features": ["default", "ring"]}
    assert enabled({"tls": ["quinn?/ring"]}, catalog.deps["app@1.0.0"], frozenset({"tls"}),  # type: ignore[arg-type]
                   weak_switches_on=True).entries == {0}


def test_a_strong_dependency_feature_also_turns_on_the_feature_named_after_the_dependency() -> None:
    # `serde = ["dep:serde", "chrono/serde"]` and `derive = ["serde/derive"]`: derive brings chrono/serde along.
    deps = [_dep("serde", "1", "optional"), _dep("chrono", "0.4", "optional")]
    features = {"serde": ["dep:serde", "chrono?/serde"], "derive": ["serde/derive"], "time": ["dep:chrono"]}
    on = enabled(features, deps, frozenset({"derive", "time"}))
    assert on.entries == {0, 1} and on.asks == {"serde": {"derive"}, "chrono": {"serde"}}
    assert enabled(features, deps, frozenset({"derive"})).asks == {"serde": {"derive"}}   # chrono stays off: weak


def test_a_feature_no_choice_can_give_fails_at_once() -> None:
    # `b` has no feature `f` in any version; trying every combination of an unrelated chain would never end.
    chain = {name: [_v(f"1.0.{i}") for i in range(15)] for name in ("c", "d", "e")}
    deps = {f"{n}@1.0.{i}": [_dep(m, "1")] for n, m in (("c", "d"), ("d", "e")) for i in range(15)}
    catalog = Catalog({"a": [_v("1.0.0", f=[])], "b": [_v("1.0.0"), _v("1.1.0")], **chain},
                      {"a@1.0.0": [_dep("c", "1")], **deps})
    graph = _resolve(catalog, ("a", "1"), ("b", "1"), env={"features": ["f"]})
    assert graph.error is not None and graph.error["code"] == "resolution_impossible"
    assert "b 1.1.0 has no feature f" in graph.error["message"]


def test_one_activation_per_links_value() -> None:
    # Two classes of a -sys crate linking one native library cannot both be built: the older dependent is used.
    sys_v = lambda v: CatalogVersion(version=v, registry_id="r", registry_slug="crates-io", status="active",  # noqa: E731
                                     links="git2")
    catalog = Catalog(
        {"app": [_v("1.0.0")], "git2": [_v("0.18.0"), _v("0.19.0")], "other": [_v("1.0.0")],
         "libgit2-sys": [sys_v("0.16.2"), sys_v("0.17.0")]},
        {"app@1.0.0": [_dep("git2", ">=0.18"), _dep("other", "1")], "git2@0.19.0": [_dep("libgit2-sys", "0.17")],
         "git2@0.18.0": [_dep("libgit2-sys", "0.16")], "other@1.0.0": [_dep("libgit2-sys", "0.16")]},
    )
    assert _set(_resolve(catalog, ("app", "1"))) == {
        ("app", "1.0.0"), ("other", "1.0.0"), ("git2", "0.18.0"), ("libgit2-sys", "0.16.2")}


def test_node_errors_of_an_abandoned_version_do_not_stay() -> None:
    # p 1.5.0 needs a crate nobody has and q =1.1.0; the root pins q =1.0.0, so p goes back to 1.4.0.
    catalog = Catalog(
        {"app": [_v("1.0.0")], "p": [_v("1.4.0"), _v("1.5.0")], "q": [_v("1.0.0"), _v("1.1.0")]},
        {"app@1.0.0": [_dep("p", "1"), _dep("q", "=1.0.0")], "p@1.5.0": [_dep("ghost", "1"), _dep("q", "=1.1.0")]},
    )
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1.0.0"), ("p", "1.4.0"), ("q", "1.0.0")}
    assert all(not n.errors for n in graph.nodes)


def test_a_dependency_from_another_registry_is_reported_not_resolved() -> None:
    dep = replace(_dep("corp-lib", "1"), details={"registry": "https://corp.example/index"})
    catalog = Catalog({"app": [_v("1.0.0")], "corp-lib": [_v("1.0.0")]}, {"app@1.0.0": [dep]})
    graph = _resolve(catalog, ("app", "1"))
    assert _set(graph) == {("app", "1.0.0")} and graph.nodes[0].errors == ["corp-lib is from another registry"]
