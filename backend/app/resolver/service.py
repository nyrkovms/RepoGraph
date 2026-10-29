"""POST /resolve (SPEC-MULTI-ECOSYSTEM.md §9.1, IMPL R2): the plugin's resolver over the catalog seen through a profile.

The graph format is the answer; the PyPI nested format of the MVP is an alias built from the same graph until the UI
moves (E7). Answers are cached by request and checked against the catalog revision of the packages they read.
"""

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from packaging.version import InvalidVersion, Version
from sqlalchemy.ext.asyncio import AsyncSession

from app.ecosystems.base import EcosystemPlugin, ResolveGraph, ResolveRoot, SourceProfile
from app.ecosystems.base import ResolveRequest as ContractRequest
from app.ecosystems.registry import UnknownEcosystem, get_plugin
from app.fetcher import sources
from app.resolver.cache import ResolveCache
from app.resolver.catalog import DbCatalog
from app.resolver.errors import (
    ResolveConflictError,
    ResolveInternalError,
    ResolveInvalidRequest,
    ResolveLimitError,
    ResolveNotFoundError,
    ResolveTimeoutError,
)
from app.schemas.resolve import (
    GraphEdge,
    GraphNode,
    GraphResolveRequest,
    GraphResolveResponse,
    ResolveRequest,
    ResolveSuccessResponse,
    ResolvedDependencyNode,
    ResolvedPackage,
)
from app.services.metrics import inc_resolve_cache_hit, inc_resolve_cache_miss, observe_resolve_latency

# Beyond the request's own timeout (enforced inside the resolver) before the answer is given up.
_TIMEOUT_MARGIN_S = 10.0


async def resolve_graph(db: AsyncSession, payload: GraphResolveRequest) -> GraphResolveResponse:
    start = time.perf_counter()
    cache = ResolveCache()
    key = ResolveCache.build_key({"format": "graph", **payload.model_dump(mode="json")})
    cached = await cache.get_valid(db, key)
    if cached is not None:
        inc_resolve_cache_hit()
        observe_resolve_latency(time.perf_counter() - start)
        return GraphResolveResponse.model_validate(cached)
    inc_resolve_cache_miss()

    plugin = _plugin(payload.ecosystem)
    profile = await _profile(db, payload.ecosystem, payload.profile)
    roots = [_root(plugin, payload.ecosystem, root.purl, root.constraint, root.extras) for root in payload.roots]
    request = ContractRequest(
        roots=roots, environment=plugin.parse_environment(payload.environment), profile=profile,
        include_dev=payload.options.include_dev, include_optional=payload.options.include_optional,
        managed=[_root(plugin, payload.ecosystem, m.purl, m.constraint, m.extras) for m in payload.managed],
        timeout_s=payload.options.timeout_s,
    )
    graph, catalog = await _run(db, plugin, request)
    lock = payload.input_mode in ("lock", "freeze")
    root_ids, hidden = _roots(graph, lock)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    response = GraphResolveResponse(
        ecosystem=payload.ecosystem,
        profile=profile.slug,
        input_mode="lock" if lock else "requirements",
        strategy=graph.strategy,
        emulates=plugin.resolver().emulates,
        nodes=[
            GraphNode(
                id=node.id, purl=plugin.coordinates.to_purl(node.ref, node.version),
                name=plugin.coordinates.display(node.ref), version=node.version, registry=node.registry_slug,
                depth=node.depth, root=node.id in root_ids, hidden=node.id in hidden,
                latest_compatible=node.latest_compatible, errors=list(node.errors),
            )
            for node in graph.nodes
        ],
        edges=[
            GraphEdge(source=edge.source, to=edge.target, constraint=edge.constraint, type=edge.dependency_type,
                      details=dict(edge.details))
            for edge in graph.edges
        ],
        conflicts=[dict(c) for c in graph.conflicts],
        warnings=[dict(w) for w in graph.warnings],
        fetched=[plugin.coordinates.to_purl(ref) for ref in graph.fetched],
        details=dict(graph.details),
        error=dict(graph.error) if graph.error else None,
        resolved_at=datetime.now(UTC),
        catalog_revision=catalog.revision,
        resolve_time_ms=elapsed_ms,
    )
    observe_resolve_latency(elapsed_ms / 1000.0)
    if graph.error is None:
        await cache.set_checked(key, response.model_dump(mode="json", by_alias=True), catalog.synced_at)
    return response


async def resolve_dependencies(db: AsyncSession, payload: ResolveRequest) -> ResolveSuccessResponse:
    """The MVP format: nested trees per root, errors as exceptions (mapped to 404/409/422/504 by the route)."""
    if payload.ecosystem != "pypi":
        raise ResolveInternalError("Only ecosystem='pypi' is supported in the nested format")
    if payload.input_mode not in {"requirements", "freeze"}:
        raise ResolveInternalError("Only input_mode='requirements' or 'freeze' is supported in the nested format")

    start = time.perf_counter()
    cache = ResolveCache()
    key = ResolveCache.build_key({"format": "nested", **payload.model_dump(mode="json")})
    cached = await cache.get_valid(db, key)
    if cached is not None:
        inc_resolve_cache_hit()
        observe_resolve_latency(time.perf_counter() - start)
        return ResolveSuccessResponse.model_validate(cached)
    inc_resolve_cache_miss()

    plugin = _plugin("pypi")
    try:
        roots = [ResolveRoot(ref=plugin.coordinates.parse(dep.name), constraint=dep.version_spec or "*", extras=tuple(dep.extras))
                 for dep in payload.dependencies]
    except ValueError as exc:
        raise ResolveInvalidRequest(str(exc)) from exc
    request = ContractRequest(
        roots=roots,
        environment=plugin.parse_environment(payload.environment.model_dump()),
        profile=await sources.default_profile(db, "pypi"),
        include_dev=payload.options.include_dev,
    )
    graph, catalog = await _run(db, plugin, request)
    _raise_for_error(graph)

    nodes = {node.id: node for node in graph.nodes}
    children: dict[str, list[Any]] = {}
    for edge in graph.edges:
        children.setdefault(edge.source, []).append(edge)
    root_ids, hidden = _roots(graph, lock=payload.input_mode == "freeze")

    resolved: dict[str, ResolvedPackage] = {}
    hidden_roots: list[str] = []
    for root, edge in zip(roots, [e for e in graph.edges if e.source == "root"], strict=True):
        node = nodes.get(edge.target)
        if node is None:
            raise ResolveConflictError(f"Unable to resolve root dependency '{root.ref.name}'")
        if node.id in hidden:
            hidden_roots.append(root.ref.name)
            continue
        resolved[root.ref.name] = ResolvedPackage(
            version=node.version,
            latest_compatible_version=node.latest_compatible,
            has_newer_compatible_version=_is_newer_version(node.latest_compatible, node.version),
            dependencies=_nested(nodes, children, node.id, payload.options.max_depth, 1, {node.ref.name}),
        )

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    observe_resolve_latency(elapsed_ms / 1000.0)
    response = ResolveSuccessResponse(
        input_mode=payload.input_mode,
        resolved=resolved,
        hidden_roots=hidden_roots,
        total_packages=len(graph.nodes),
        total_versions=len(graph.nodes),
        conflicts=[],
        resolve_strategy="resolvelib",
        resolve_time_ms=elapsed_ms,
    )
    await cache.set_checked(key, response.model_dump(mode="json"), catalog.synced_at)
    return response


async def _run(db: AsyncSession, plugin: EcosystemPlugin, request: ContractRequest) -> tuple[ResolveGraph, DbCatalog]:
    catalog = DbCatalog(db, plugin)
    try:
        graph = await asyncio.wait_for(plugin.resolver().resolve(request, catalog), request.timeout_s + _TIMEOUT_MARGIN_S)
    except TimeoutError:
        graph = ResolveGraph(strategy="", nodes=[], edges=[], conflicts=[], warnings=[], fetched=[],
                             error={"code": "resolve_timeout", "message": f"resolve took longer than {request.timeout_s:g} s"})
    return graph, catalog


def _roots(graph: ResolveGraph, lock: bool) -> tuple[set[str], set[str]]:
    """Root node ids, and in lock mode those reachable from another root (R2: `hidden`)."""
    root_ids = {edge.target for edge in graph.edges if edge.source == "root"}
    if not lock:
        return root_ids, set()
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.source != "root":
            adjacency.setdefault(edge.source, []).append(edge.target)
    hidden: set[str] = set()
    for root in root_ids:
        seen, stack = {root}, list(adjacency.get(root, []))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, []))
        hidden |= (seen - {root}) & root_ids
    return root_ids, hidden


def _nested(nodes, children, node_id: str, max_depth: int, depth: int, seen: set[str]) -> list[ResolvedDependencyNode]:
    if depth > max_depth:
        return []
    result = []
    for edge in children.get(node_id, []):
        child = nodes[edge.target]
        name = child.ref.name
        result.append(
            ResolvedDependencyNode(
                name=name,
                version=child.version,
                constraint=edge.constraint if edge.constraint != "*" else "",
                dependency_type=edge.dependency_type,
                extra_name=edge.details.get("extra_name"),
                environment_markers=edge.details.get("environment_markers"),
                dependencies=[] if name in seen else _nested(nodes, children, child.id, max_depth, depth + 1, seen | {name}),
            )
        )
    return result


def _raise_for_error(graph: ResolveGraph) -> None:
    if graph.error is None:
        return
    code, message = graph.error.get("code"), str(graph.error.get("message"))
    if code == "not_found":
        raise ResolveNotFoundError(str((graph.error.get("packages") or ["?"])[0]))
    if code == "resolver_conflict":
        raise ResolveConflictError(message, details={"causes": [dict(c) for c in graph.conflicts]})
    if code == "resolve_timeout":
        raise ResolveTimeoutError(message)
    if code == "resolve_limit_exceeded":
        raise ResolveLimitError(message, details=dict(graph.error))
    raise ResolveInternalError(message)


def _plugin(ecosystem: str) -> EcosystemPlugin:
    try:
        return get_plugin(ecosystem)
    except UnknownEcosystem:
        raise ResolveInvalidRequest(f"ecosystem {ecosystem!r} is not supported") from None


async def _profile(db: AsyncSession, ecosystem: str, slug: str | None) -> SourceProfile:
    profile = await sources.profile_by_slug(db, ecosystem, slug)
    if profile is None:
        raise ResolveInvalidRequest(f"profile {slug!r} not found for ecosystem {ecosystem!r}")
    return profile


def _root(plugin: EcosystemPlugin, ecosystem: str, purl: str, constraint: str, extras: Sequence[str]) -> ResolveRoot:
    """A root by purl; a version in the purl pins it when no constraint is given."""
    try:
        ref, version = plugin.coordinates.from_purl(purl)
    except ValueError as exc:
        raise ResolveInvalidRequest(f"invalid root {purl!r}: {exc}") from exc
    if ref.ecosystem != ecosystem:
        raise ResolveInvalidRequest(f"root {purl!r} is not in ecosystem {ecosystem!r}")
    if version and constraint in ("", "*"):
        constraint = f"=={version}"
    return ResolveRoot(ref=ref, constraint=constraint or "*", extras=tuple(extras))


def _is_newer_version(latest_compatible_version: str | None, selected_version: str) -> bool:
    if not latest_compatible_version:
        return False
    try:
        return Version(latest_compatible_version) > Version(selected_version)
    except InvalidVersion:
        return latest_compatible_version != selected_version
