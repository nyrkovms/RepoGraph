"""Graph post-processing of POST /resolve (IMPL C2, R2): hidden roots, roots by purl, the nested alias."""

import pytest

from app.ecosystems.base import PackageRef, ResolvedEdge, ResolvedNode, ResolveGraph
from app.ecosystems.registry import get_plugin, load_builtin_plugins
from app.resolver.errors import ResolveInvalidRequest
from app.resolver.service import _nested, _root, _roots

load_builtin_plugins()
PLUGIN = get_plugin("pypi")


def _node(node_id: str, name: str, version: str = "1.0") -> ResolvedNode:
    return ResolvedNode(id=node_id, ref=PackageRef(ecosystem="pypi", namespace="", name=name), version=version,
                        registry_slug="pypi", depth=1)


def _edge(source: str, target: str, constraint: str = "*", **details: object) -> ResolvedEdge:
    return ResolvedEdge(source=source, target=target, constraint=constraint, dependency_type="runtime", details=details)


# flask -> werkzeug -> markupsafe -> werkzeug (a cycle); werkzeug and pydantic are roots too.
GRAPH = ResolveGraph(
    strategy="pip-resolvelib",
    nodes=[_node("n1", "flask"), _node("n2", "werkzeug"), _node("n3", "pydantic"), _node("n4", "markupsafe")],
    edges=[_edge("root", "n1"), _edge("root", "n2"), _edge("root", "n3"),
           _edge("n1", "n2", ">=3", extra_name="async"), _edge("n2", "n4"), _edge("n4", "n2")],
    conflicts=[], warnings=[], fetched=[],
)


def test_lock_mode_hides_roots_reached_from_other_roots() -> None:
    assert _roots(GRAPH, lock=False) == ({"n1", "n2", "n3"}, set())
    assert _roots(GRAPH, lock=True) == ({"n1", "n2", "n3"}, {"n2"})


def test_nested_alias_stops_at_cycles_and_depth() -> None:
    nodes = {n.id: n for n in GRAPH.nodes}
    children: dict[str, list[ResolvedEdge]] = {}
    for edge in GRAPH.edges:
        children.setdefault(edge.source, []).append(edge)
    tree = _nested(nodes, children, "n1", max_depth=10, depth=1, seen={"flask"})
    assert [(d.name, d.constraint, d.extra_name) for d in tree] == [("werkzeug", ">=3", "async")]
    markupsafe = tree[0].dependencies[0]
    assert markupsafe.name == "markupsafe"
    assert [(d.name, d.dependencies) for d in markupsafe.dependencies] == [("werkzeug", [])]   # cycle cut
    assert _nested(nodes, children, "n1", max_depth=1, depth=1, seen={"flask"})[0].dependencies == []


def test_root_by_purl() -> None:
    assert _root(PLUGIN, "pypi", "pkg:pypi/Flask", ">=3", ["async"]).constraint == ">=3"
    pinned = _root(PLUGIN, "pypi", "pkg:pypi/torch@2.5.1%2Bcu121", "*", [])
    assert (pinned.ref.name, pinned.constraint) == ("torch", "==2.5.1+cu121")
    with pytest.raises(ResolveInvalidRequest):
        _root(PLUGIN, "pypi", "pkg:npm/left-pad", "*", [])
