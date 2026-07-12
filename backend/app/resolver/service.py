import asyncio
import time
from functools import lru_cache

import httpx
from packaging.version import InvalidVersion, Version
from resolvelib import BaseReporter, Resolver
from resolvelib.resolvers import ResolutionImpossible, ResolutionTooDeep
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.connectors.pypi import PyPIConnector
from app.db import SessionLocal
from app.fetcher.pipeline import run_sync_pipeline
from app.resolver.cache import ResolveCache
from app.resolver.errors import (
    ResolveConflictError,
    ResolveInternalError,
)
from app.resolver.models import ResolverRequirement
from app.resolver.provider import PyPIProvider
from app.schemas.resolve import ResolveRequest, ResolveSuccessResponse, ResolvedDependencyNode, ResolvedPackage
from app.services.metrics import (
    inc_resolve_cache_hit,
    inc_resolve_cache_miss,
    observe_resolve_latency,
)


@lru_cache(maxsize=1)
def _sync_session_factory():
    settings = get_settings()
    engine = create_engine(settings.sync_db_url, pool_pre_ping=True)
    return sessionmaker(bind=engine)


async def resolve_dependencies(payload: ResolveRequest) -> ResolveSuccessResponse:
    if payload.ecosystem != "pypi":
        raise ResolveInternalError("Only ecosystem='pypi' is supported in MVP")
    if payload.input_mode not in {"requirements", "freeze"}:
        raise ResolveInternalError("Only input_mode='requirements' or 'freeze' is supported in MVP")

    start = time.perf_counter()
    cache = ResolveCache()
    cache_key = ResolveCache.build_key(payload.model_dump(mode="json"))
    cached = await cache.get(cache_key)
    if cached:
        inc_resolve_cache_hit()
        observe_resolve_latency(time.perf_counter() - start)
        return ResolveSuccessResponse.model_validate(cached)
    inc_resolve_cache_miss()

    result = await asyncio.to_thread(_resolve_sync, payload)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    observe_resolve_latency(elapsed_ms / 1000.0)
    result.resolve_time_ms = elapsed_ms

    await cache.set(cache_key, result.model_dump(mode="json"))
    return result


def _resolve_sync(payload: ResolveRequest) -> ResolveSuccessResponse:
    session_factory = _sync_session_factory()

    def lazy_fetch_sync(name: str) -> str:
        return asyncio.run(_lazy_fetch_async(name))

    provider = PyPIProvider(
        session_factory=session_factory,
        environment=_environment_dict(payload),
        lazy_fetch=lazy_fetch_sync,
        include_dev=payload.options.include_dev,
    )
    resolver = Resolver(provider, BaseReporter())

    requirements = [
        ResolverRequirement.from_input(dep.name, dep.version_spec, dep.extras)
        for dep in payload.dependencies
    ]

    try:
        result = resolver.resolve(requirements, max_rounds=payload.options.max_rounds)
    except ResolutionImpossible as exc:
        raise ResolveConflictError("Dependency conflict", details=_extract_conflicts(exc)) from exc
    except ResolutionTooDeep as exc:
        raise ResolveConflictError("Resolution exceeded maximum rounds", details={"max_rounds": payload.options.max_rounds}) from exc

    mapping = result.mapping
    resolved_all_roots: dict[str, ResolvedPackage] = {}

    for root in requirements:
        candidate = mapping.get(root.name)
        if not candidate:
            raise ResolveConflictError(f"Unable to resolve root dependency '{root.name}'")

        deps = _serialize_dependencies(
            provider=provider,
            mapping=mapping,
            candidate=candidate,
            max_depth=payload.options.max_depth,
            depth=1,
            seen={root.name},
        )
        latest_compatible_version = provider.get_latest_compatible_version(root.name)
        resolved_all_roots[root.name] = ResolvedPackage(
            version=candidate.version_str,
            latest_compatible_version=latest_compatible_version,
            has_newer_compatible_version=_is_newer_version(latest_compatible_version, candidate.version_str),
            dependencies=deps,
        )

    hidden_roots: list[str] = []
    if payload.input_mode == "freeze":
        visible_root_names, hidden_roots = _select_visible_roots(requirements, resolved_all_roots)
    else:
        visible_root_names = [root.name for root in requirements]

    resolved = {
        root_name: resolved_all_roots[root_name]
        for root_name in visible_root_names
        if root_name in resolved_all_roots
    }

    return ResolveSuccessResponse(
        input_mode=payload.input_mode,
        resolved=resolved,
        hidden_roots=hidden_roots,
        total_packages=len(mapping),
        total_versions=len(mapping),
        conflicts=[],
        resolve_strategy="resolvelib",
        resolve_time_ms=0.0,
    )


def _serialize_dependencies(provider, mapping, candidate, max_depth: int, depth: int, seen: set[str]) -> list[ResolvedDependencyNode]:
    deps = provider.get_dependencies(candidate)
    nodes: list[ResolvedDependencyNode] = []

    if depth > max_depth:
        return nodes

    for req in deps:
        selected = mapping.get(req.name)
        if not selected:
            continue

        if req.name in seen:
            nodes.append(
                ResolvedDependencyNode(
                    name=req.name,
                    version=selected.version_str,
                    constraint=str(req.specifier),
                    dependency_type=req.dependency_type,
                    extra_name=req.extra_name,
                    environment_markers=req.environment_markers,
                    dependencies=[],
                )
            )
            continue

        children = _serialize_dependencies(
            provider=provider,
            mapping=mapping,
            candidate=selected,
            max_depth=max_depth,
            depth=depth + 1,
            seen=seen | {req.name},
        )
        nodes.append(
            ResolvedDependencyNode(
                name=req.name,
                version=selected.version_str,
                constraint=str(req.specifier),
                dependency_type=req.dependency_type,
                extra_name=req.extra_name,
                environment_markers=req.environment_markers,
                dependencies=children,
            )
        )

    return nodes


async def _lazy_fetch_async(name: str) -> str:
    connector = PyPIConnector(timeout_seconds=10.0)

    try:
        metadata = await connector.fetch_package_metadata(name)
    except PackageNotFound:
        return "not_found"
    except httpx.TimeoutException:
        return "timeout"
    except Exception:  # noqa: BLE001
        return "error"

    async with SessionLocal() as db:
        try:
            await run_sync_pipeline(metadata, db)
            return "fetched"
        except Exception:  # noqa: BLE001
            return "error"


def _environment_dict(payload: ResolveRequest) -> dict[str, str]:
    env = payload.environment
    python_version = env.python_version
    python_full_version = env.python_full_version or f"{python_version}.0"

    return {
        "python_version": python_version,
        "python_full_version": python_full_version,
        "sys_platform": env.sys_platform,
        "os_name": env.os_name,
        "platform_machine": env.platform_machine,
        "platform_system": env.platform_system,
        "implementation_name": env.implementation_name,
    }


def _extract_conflicts(exc: ResolutionImpossible) -> dict:
    causes = []
    for cause in getattr(exc, "causes", []):
        requirement = getattr(cause, "requirement", None)
        parent = getattr(cause, "parent", None)
        causes.append(
            {
                "requirement": str(requirement) if requirement else None,
                "parent": str(parent) if parent else None,
            }
        )
    return {"causes": causes}


def _collect_dependency_names(nodes: list[ResolvedDependencyNode]) -> set[str]:
    names: set[str] = set()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        names.add(node.name)
        stack.extend(node.dependencies)
    return names


def _select_visible_roots(
    requirements: list[ResolverRequirement],
    resolved_by_root: dict[str, ResolvedPackage],
) -> tuple[list[str], list[str]]:
    requested_order = [requirement.name for requirement in requirements]
    requested_set = set(requested_order)
    hidden_roots: set[str] = set()

    for root_name in requested_order:
        pkg = resolved_by_root.get(root_name)
        if not pkg:
            continue
        reachable_requested = _collect_dependency_names(pkg.dependencies) & requested_set
        reachable_requested.discard(root_name)
        hidden_roots.update(reachable_requested)

    visible_roots = [root_name for root_name in requested_order if root_name not in hidden_roots]
    return visible_roots, [root_name for root_name in requested_order if root_name in hidden_roots]


def _is_newer_version(latest_compatible_version: str | None, selected_version: str) -> bool:
    if not latest_compatible_version:
        return False
    try:
        return Version(latest_compatible_version) > Version(selected_version)
    except InvalidVersion:
        return latest_compatible_version != selected_version
