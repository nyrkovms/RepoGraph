"""The PyPI resolver behind the contract (IMPL C2) over an in-memory catalog."""

import asyncio
from collections.abc import Sequence

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
from app.ecosystems.pypi.environment import PythonEnvironment, parse_environment
from app.ecosystems.pypi.pep508 import parse_pep508
from app.ecosystems.pypi.resolver import NOT_FOUND_PREFIX, RESOLVER, UNKNOWN_DEPENDENCIES

PROFILE = SourceProfile(ecosystem="pypi", slug="default", registries=[])
ANY_WHEEL = ("x-py3-none-any.whl",)


def _v(version: str, status: str = "active", python: str | None = None, files: Sequence[str] = ANY_WHEEL,
       registry: str = "pypi") -> CatalogVersion:
    return CatalogVersion(version=version, registry_id=f"id-{registry}", registry_slug=registry,
                          status=status,  # type: ignore[arg-type]
                          engines={"python": python} if python else None, artifacts=tuple(files))


def _dep(spec: str) -> NormalizedDependency:
    parsed = parse_pep508(spec)
    return NormalizedDependency(
        target=PackageRef(ecosystem="pypi", namespace="", name=parsed.dependency_name),
        constraint=parsed.version_constraint, dependency_type=parsed.dependency_type,  # type: ignore[arg-type]
        raw_spec=parsed.raw_spec, extra_name=parsed.extra_name, environment_markers=parsed.environment_markers,
    )


class Catalog:
    def __init__(self, versions: dict[str, list[CatalogVersion]],
                 deps: dict[tuple[str, str], list[str] | None] | None = None,
                 fetchable: dict[str, list[CatalogVersion]] | None = None, delay: float = 0.0) -> None:
        self.data = versions
        self.deps = deps or {}
        self.fetchable = fetchable or {}
        self.delay = delay
        self.ensured: list[str] = []

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.data.get(ref.name, [])

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        specs = self.deps.get((ref.name, version), [])
        return None if specs is None else [_dep(s) for s in specs]

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        self.ensured.append(ref.name)
        if ref.name in self.fetchable:
            self.data[ref.name] = self.fetchable.pop(ref.name)
            return True
        return False


def _resolve(catalog: Catalog, *roots: tuple[str, str] | tuple[str, str, list[str]],
             env: PythonEnvironment | None = None, **limits: object) -> ResolveGraph:
    request = ResolveRequest(
        roots=[ResolveRoot(ref=PackageRef(ecosystem="pypi", namespace="", name=r[0]), constraint=r[1],
                           extras=tuple(r[2]) if len(r) > 2 else ()) for r in roots],  # type: ignore[misc]
        environment=env or parse_environment({}), profile=PROFILE, **limits,  # type: ignore[arg-type]
    )
    return asyncio.run(RESOLVER.resolve(request, catalog))


def _chosen(graph: ResolveGraph) -> dict[str, str]:
    assert graph.error is None, graph.error
    return {node.ref.name: node.version for node in graph.nodes}


def test_resolves_a_chain_into_nodes_and_edges() -> None:
    catalog = Catalog({"a": [_v("1.0"), _v("2.0")], "b": [_v("1.0"), _v("1.1", registry="mirror")]},
                      {("a", "2.0"): ["b>=1"]})
    graph = _resolve(catalog, ("a", ">=1"))
    assert graph.strategy == "pip-resolvelib"
    assert [(n.id, n.ref.name, n.version, n.depth, n.registry_slug) for n in graph.nodes] == [
        ("n1", "a", "2.0", 1, "pypi"), ("n2", "b", "1.1", 2, "mirror")]
    assert [(e.source, e.target, e.constraint, e.dependency_type) for e in graph.edges] == [
        ("root", "n1", ">=1", "runtime"), ("n1", "n2", ">=1", "runtime")]
    assert graph.nodes[0].latest_compatible == "2.0"
    assert graph.nodes[1].latest_compatible is None   # only roots carry it


def test_prereleases_follow_pip() -> None:
    catalog = Catalog({"a": [_v("1.0"), _v("2.0b1")], "c": [_v("1.0a1")]})
    assert _chosen(_resolve(catalog, ("a", "*"))) == {"a": "1.0"}
    assert _chosen(_resolve(catalog, ("a", ">=2.0b1"))) == {"a": "2.0b1"}
    assert _chosen(_resolve(catalog, ("c", "*"))) == {"c": "1.0a1"}     # nothing else matches


def test_yanked_only_when_pinned() -> None:
    catalog = Catalog({"a": [_v("1.0"), _v("1.1", status="yanked"), _v("1.2", status="removed")]})
    assert _chosen(_resolve(catalog, ("a", ">=1"))) == {"a": "1.0"}
    assert _chosen(_resolve(catalog, ("a", "==1.1"))) == {"a": "1.1"}
    assert _resolve(catalog, ("a", "==1.2")).error["code"] == "resolver_conflict"
    # PEP 592: an exact pin anywhere allows the yanked version, other specifiers may join it.
    pinned = Catalog({"a": [_v("1.0"), _v("1.1", status="yanked")], "b": [_v("1.0")]}, {("b", "1.0"): ["a>=1"]})
    assert _chosen(_resolve(pinned, ("a", "==1.1"), ("b", "*"))) == {"a": "1.1", "b": "1.0"}


def test_requires_python_and_platform_filter_candidates() -> None:
    catalog = Catalog({"a": [_v("1.0"), _v("1.5"), _v("2.0", python=">=3.13")],
                       "w": [_v("1.0"), _v("2.0", files=("w-2.0-cp312-cp312-win_amd64.whl",))]})
    graph = _resolve(catalog, ("a", "<1.5"))
    assert _chosen(graph) == {"a": "1.0"}
    assert graph.nodes[0].latest_compatible == "1.5"    # the constraint is ignored, the environment is not
    assert _chosen(_resolve(catalog, ("w", "*"))) == {"w": "1.0"}
    windows = parse_environment({"python_version": "3.12", "os": "windows"})
    assert _chosen(_resolve(catalog, ("w", "*"), env=windows)) == {"w": "2.0"}


def test_requires_python_uses_the_full_version_and_python_only_means_linux() -> None:
    catalog = Catalog({"a": [_v("1.0"), _v("2.0", python=">=3.12.1")],
                       "w": [_v("1.0", files=("w-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",))]})
    assert _chosen(_resolve(catalog, ("a", "*"), env=parse_environment({"python_version": "3.12"}))) == {"a": "1.0"}
    newer = parse_environment({"python_version": "3.12", "python_full_version": "3.12.4"})
    assert _chosen(_resolve(catalog, ("a", "*"), env=newer)) == {"a": "2.0"}
    assert _chosen(_resolve(catalog, ("w", "*"), env=parse_environment({"python_version": "3.12"}))) == {"w": "1.0"}


def test_local_version_matches_its_public_pin() -> None:
    catalog = Catalog({"torch": [_v("2.5.1+cu121", registry="pytorch-cu121"), _v("2.4.0")]})
    graph = _resolve(catalog, ("torch", "==2.5.1"))
    assert _chosen(graph) == {"torch": "2.5.1+cu121"}
    assert graph.nodes[0].registry_slug == "pytorch-cu121"


EXTRAS_ROWS = [
    "zipp>=3.20",
    "requests[socks]>=2",
    'pytest>=6; extra == "test"',
    'sphinx; python_version < "3.10" and extra == "doc"',
    'furo; extra == "doc" or extra == "all"',
]


def _extras_catalog() -> Catalog:
    names = ["zipp", "requests", "pytest", "sphinx", "furo", "pysocks"]
    return Catalog({"importlib-metadata": [_v("8.7.0")], **{n: [_v("9.0")] for n in names}},
                   {("importlib-metadata", "8.7.0"): EXTRAS_ROWS, ("requests", "9.0"): ['pysocks; extra == "socks"']})


@pytest.mark.parametrize(
    ("extras", "expected"),
    [
        ([], {"importlib-metadata", "zipp", "requests", "pysocks"}),
        (["test"], {"importlib-metadata", "zipp", "requests", "pysocks", "pytest"}),
        (["doc"], {"importlib-metadata", "zipp", "requests", "pysocks", "furo"}),   # sphinx needs python < 3.10
        (["all"], {"importlib-metadata", "zipp", "requests", "pysocks", "furo"}),
    ],
)
def test_optional_dependencies_follow_requested_extras_and_markers(extras: list[str], expected: set[str]) -> None:
    graph = _resolve(_extras_catalog(), ("importlib-metadata", "*", extras))
    assert set(_chosen(graph)) == expected


def test_edges_keep_extra_and_markers() -> None:
    graph = _resolve(_extras_catalog(), ("importlib-metadata", "*", ["test"]))
    ids = {n.ref.name: n.id for n in graph.nodes}
    pytest_edge = next(e for e in graph.edges if e.target == ids["pytest"])
    assert pytest_edge.dependency_type == "optional"
    assert pytest_edge.details["extra_name"] == "test"
    assert pytest_edge.details["environment_markers"]["expression"] == 'extra == "test"'
    requests_edge = next(e for e in graph.edges if e.target == ids["requests"])
    assert requests_edge.details["extras"] == ["socks"]


def test_unknown_dependencies_mark_the_node_and_keep_the_graph() -> None:
    catalog = Catalog({"a": [_v("1.0")], "b": [_v("1.0")]}, {("a", "1.0"): ["b"], ("b", "1.0"): None})
    graph = _resolve(catalog, ("a", "*"))
    b = next(n for n in graph.nodes if n.ref.name == "b")
    assert b.errors == [UNKNOWN_DEPENDENCIES]
    assert graph.warnings == [{"code": "dependencies_unknown", "node": b.id,
                               "message": "dependencies of b 1.0 are unknown"}]


def test_unknown_package_is_fetched_lazily() -> None:
    catalog = Catalog({"a": [_v("1.0")]}, {("a", "1.0"): ["new-pkg>=1"]}, fetchable={"new-pkg": [_v("1.2")]})
    graph = _resolve(catalog, ("a", "*"))
    assert _chosen(graph) == {"a": "1.0", "new-pkg": "1.2"}
    assert [ref.name for ref in graph.fetched] == ["new-pkg"]


def test_dependency_no_source_has_gives_a_partial_graph() -> None:
    catalog = Catalog({"a": [_v("1.0")], "b": [_v("1.0")]}, {("a", "1.0"): ["ghost", "b"]})
    graph = _resolve(catalog, ("a", "*"))
    assert _chosen(graph) == {"a": "1.0", "b": "1.0"}
    assert graph.nodes[0].errors == [NOT_FOUND_PREFIX + "ghost"]
    assert graph.warnings[0]["code"] == "dependency_not_found" and graph.warnings[0]["package"] == "ghost"


def test_root_no_source_has_is_not_found() -> None:
    graph = _resolve(Catalog({}), ("ghost", "*"))
    assert graph.error == {"code": "not_found", "message": "no source of the profile has ghost", "packages": ["ghost"]}
    assert graph.nodes == []


def test_extras_asked_after_the_package_is_pinned_still_count() -> None:
    # `requests` is pinned through the root before `a` asks for `requests[socks]`: pip keeps the extra.
    catalog = Catalog({"a": [_v("1.0")], "requests": [_v("2.0")], "pysocks": [_v("1.7")]},
                      {("a", "1.0"): ["requests[socks]"], ("requests", "2.0"): ['pysocks; extra == "socks"']})
    for roots in ((("requests", "*"), ("a", "*")), (("a", "*"), ("requests", "*"))):
        graph = _resolve(catalog, *roots)
        assert _chosen(graph) == {"a": "1.0", "requests": "2.0", "pysocks": "1.7"}
        ids = {n.ref.name: n.id for n in graph.nodes}
        assert (ids["requests"], ids["pysocks"]) in {(e.source, e.target) for e in graph.edges}
        assert len(graph.nodes) == 3   # requests[socks] is the requests node


def test_conflict_reports_its_causes() -> None:
    catalog = Catalog({"a": [_v("1.0")], "b": [_v("0.5"), _v("2.0")]}, {("a", "1.0"): ["b<1"]})
    graph = _resolve(catalog, ("a", "*"), ("b", ">=2"))
    assert graph.error["code"] == "resolver_conflict"
    assert {"requirement": "b<1", "parent": "a==1.0"} in graph.conflicts


def test_limits() -> None:
    chain = Catalog({"a": [_v("1.0")], "b": [_v("1.0")], "c": [_v("1.0")]}, {("a", "1.0"): ["b"], ("b", "1.0"): ["c"]})
    graph = _resolve(chain, ("a", "*"), max_nodes=2)
    assert (graph.error["code"], graph.error["limit"], graph.error["value"]) == ("resolve_limit_exceeded", "nodes", 2)

    missing = Catalog({}, fetchable={"x": [_v("1.0")]})
    graph = _resolve(missing, ("x", "*"), max_lazy_fetches=0)
    assert (graph.error["code"], graph.error["limit"]) == ("resolve_limit_exceeded", "lazy_fetches")
    assert missing.ensured == []


def test_timeout_gives_an_error_not_an_exception() -> None:
    slow = Catalog({"a": [_v("1.0")]}, delay=0.5)
    graph = _resolve(slow, ("a", "*"), timeout_s=0.1)
    assert graph.error["code"] == "resolve_timeout"


def test_catalog_failure_is_a_graph_error() -> None:
    class Broken(Catalog):
        async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
            raise ConnectionError("database went away")

    graph = _resolve(Broken({}), ("a", "*"))
    assert graph.error["code"] == "upstream_error" and "database went away" in graph.error["message"]


def test_timeout_waits_for_the_catalog_call_it_cut_short() -> None:
    finished: list[str] = []

    class Slow(Catalog):
        async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
            try:
                await asyncio.sleep(0.3)
            finally:
                await asyncio.sleep(0.05)   # cleanup that must end before the session is used again
                finished.append(ref.name)
            return []

    graph = _resolve(Slow({}), ("a", "*"), timeout_s=0.05)
    assert graph.error["code"] == "resolve_timeout"
    assert finished == ["a"]
