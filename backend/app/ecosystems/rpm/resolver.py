"""RPM resolve through rpm-worker's libsolv (SPEC-MULTI-ECOSYSTEM.md §9.8): the pool is the profile's sources of the
request's distro (`environment.distro`, else the roots' namespace; `environment.repos` narrows it), the job installs
the roots by name or capability, and the answer is the transaction: one node per NEVRA, an edge per capability
requirement met inside it. Packages of the transaction the catalog lacks are queued for it as `discovered`.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from app.ecosystems.base import (
    CatalogAccess,
    PackageRef,
    RegistryConfig,
    ResolvedEdge,
    ResolvedNode,
    ResolveGraph,
    ResolveRequest,
)
from app.ecosystems.rpm import client
from app.ecosystems.rpm.coordinates import RpmCoordinates
from app.ecosystems.rpm.environment import RpmEnvironment

logger = logging.getLogger(__name__)
_COORDINATES = RpmCoordinates()


def _sources(request: ResolveRequest, distro: str, env: RpmEnvironment) -> list[RegistryConfig]:
    mine = [r for r in request.profile.registries if r.config.get("namespace") == distro]
    return [r for r in mine if r.slug in env.repos] if env.repos else mine


def _depths(nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]],
            roots: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    below: dict[str, list[str]] = {}
    for edge in edges:
        below.setdefault(str(edge["from"]), []).append(str(edge["to"]))
    depth = {str(r["nevra"]): 1 for r in roots}
    queue = deque(depth)
    while queue:
        current = queue.popleft()
        for child in below.get(current, []):
            if child not in depth:
                depth[child] = depth[current] + 1
                queue.append(child)
    return {str(n["nevra"]): depth.get(str(n["nevra"]), 0) for n in nodes}


def _queue_discovered(purls: list[str]) -> None:
    if not purls:
        return
    try:
        from app.celery_app import celery_app
        celery_app.send_task("app.tasks.fetch_packages.fetch_packages", args=[purls, "discovered"])
    except Exception:  # noqa: BLE001 - the catalog fills later; the resolve answer stands
        logger.warning("Could not queue %d discovered RPM packages", len(purls), exc_info=True)


class RpmResolver:
    emulates = "dnf 4"
    strategy = "libsolv"

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        graph = ResolveGraph(strategy=self.strategy, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[])
        env = request.environment if isinstance(request.environment, RpmEnvironment) else RpmEnvironment()
        namespaces = {r.ref.namespace for r in request.roots}
        distro = env.distro or (next(iter(namespaces)) if len(namespaces) == 1 else None)
        if distro is None:
            graph.error = {"code": "invalid_request",
                           "message": "roots of several distros: give environment.distro or roots of one distro"}
            return graph
        sources = _sources(request, distro, env)
        if not sources:
            graph.error = {"code": "not_found", "message": f"no source of the profile serves {distro}"}
            return graph
        payload = {
            "sources": [client.source_payload(r) for r in sources], "arch": env.arch, "weak_deps": env.weak_deps,
            "roots": [{"spec": r.ref.name, "constraint": "" if r.constraint in ("", "*") else r.constraint}
                      for r in request.roots],
            "module_streams": dict(env.module_streams), "disabled_modules": list(env.disabled_modules),
            "module_install": list(env.module_install),
        }
        try:
            answer = await client.call("rpm.resolve", payload, timeout=request.timeout_s)
        except client.WorkerError as exc:
            graph.error = {"code": "upstream_error", "message": str(exc)}
            return graph
        problems = answer.get("problems") or []
        if problems:
            first = problems[0]
            graph.conflicts = [dict(p) for p in problems]
            graph.error = {"code": str(first.get("code") or "unsolvable"), "message": str(first.get("message")),
                           "problems": [dict(p) for p in problems]}
            return graph
        nodes: list[Mapping[str, Any]] = answer.get("nodes") or []
        edges: list[Mapping[str, Any]] = answer.get("edges") or []
        roots: list[Mapping[str, Any]] = answer.get("roots") or []
        if len(nodes) > request.max_nodes:
            graph.error = {"code": "resolve_limit_exceeded", "message": f"resolve exceeded {request.max_nodes} nodes",
                           "limit": "nodes", "value": request.max_nodes}
            return graph
        depths = _depths(nodes, edges, roots)
        ordered = sorted(nodes, key=lambda n: (depths[str(n["nevra"])] or 1_000, str(n["name"]), str(n["arch"])))
        ids = {str(n["nevra"]): f"n{i}" for i, n in enumerate(ordered, start=1)}
        for node in ordered:
            graph.nodes.append(ResolvedNode(
                id=ids[str(node["nevra"])], ref=PackageRef("rpm", distro, str(node["name"])), version=str(node["evr"]),
                registry_slug=str(node["repo"]), depth=depths[str(node["nevra"])],
                details={"arch": node["arch"], "nevra": node["nevra"]}))
        constraints = {r.ref.name: r.constraint for r in request.roots}
        for root in roots:
            spec = str(root["spec"])
            graph.edges.append(ResolvedEdge(source="root", target=ids[str(root["nevra"])],
                                            constraint=constraints.get(spec.split(" ")[0], ""), dependency_type="runtime"))
        for edge in edges:
            graph.edges.append(ResolvedEdge(
                source=ids[str(edge["from"])], target=ids[str(edge["to"])], constraint=str(edge["capability"]),
                dependency_type="runtime" if edge.get("kind") == "requires" else "optional",
                details={"capability": edge["capability"], "kind": edge.get("kind")}))
        graph.details = {"modules": dict(answer.get("modules") or {}), "distro": distro, "arch": env.arch}
        # Packages of the transaction the catalog does not have yet: queued for it as `discovered`, off the loop.
        refs = sorted({PackageRef("rpm", distro, str(n["name"])) for n in nodes}, key=lambda r: r.name)
        unknown = [r for r in refs if not await catalog.versions(r, request.profile)]
        await asyncio.to_thread(_queue_discovered, [_COORDINATES.to_purl(r) for r in unknown])
        return graph


RESOLVER = RpmResolver()
