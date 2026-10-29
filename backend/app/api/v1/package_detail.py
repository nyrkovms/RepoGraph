"""One package: purl addressing `/packages/{ecosystem}/{coordinates}/-/…` (SPEC-MULTI-ECOSYSTEM.md §11.1) and the
PyPI routes `/packages/pypi/{name}/…`, which stay as deprecated aliases until the UI moves (E7)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from packaging.markers import Marker
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.packages import _decode_cursor_or_422
from app.db import get_db_session
from app.ecosystems import purl as purls
from app.ecosystems.base import EcosystemPlugin, PackageRef, RegistryConfig, SourceProfile
from app.ecosystems.registry import UnknownEcosystem, get_plugin
from app.fetcher import sources
from app.fetcher.lazy import LazyFetcher
from app.fetcher.pipeline import (
    ensure_version_dependencies,
    find_package,
    resolve_dependency_links,
    select_best_version,
)
from app.models.artifact import Artifact
from app.models.dependency import Dependency
from app.models.enums import DependencyType, PackageVersionStatus
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.version import PackageVersion, VersionSource
from app.models.vulnerability import PackageVulnerability, Vulnerability
from app.schemas.packages import (
    ArtifactItem,
    ArtifactListResponse,
    DependencyNode,
    DependencyTreeResponse,
    PackageDetailResponse,
    PackageSourceItem,
    VersionDetailResponse,
    VersionItem,
    VersionListResponse,
    VersionSourceItem,
    VulnerabilityItem,
    VulnerabilityListResponse,
)
from app.services import visibility
from app.services.cursor import encode_cursor, parse_iso_datetime, parse_uuid

router = APIRouter()
FULL_TREE_MAX_DEPTH = 200
API_PREFIX = "/api/v1"
_COORDS = "/packages/{ecosystem}/{coordinates:path}/-"


@dataclass(slots=True)
class _Target:
    plugin: EcosystemPlugin
    ref: PackageRef
    package: Package

    @property
    def display(self) -> str:
        return self.plugin.coordinates.display(self.ref)


# --- purl --------------------------------------------------------------------------------------------------


@router.get("/purl/{purl:path}", status_code=307)
async def by_purl(purl: str) -> RedirectResponse:
    """Package or version by purl: a redirect to its canonical path. Qualifiers are ignored."""
    try:
        parsed = purls.parse(purl)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        plugin = get_plugin(purls.ecosystem_for(parsed.type))
    except (UnknownEcosystem, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"Ecosystem of '{purl}' is not supported") from exc
    try:
        ref, version = plugin.coordinates.from_purl(purl)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(canonical_path(plugin, ref, version), status_code=307)


def canonical_path(plugin: EcosystemPlugin, ref: PackageRef, version: str | None = None) -> str:
    path = f"{API_PREFIX}/packages/{ref.ecosystem}/{quote(plugin.coordinates.display(ref), safe='/@:')}/-/"
    return f"{path}versions/{quote(version, safe='')}" if version else path


# --- package -----------------------------------------------------------------------------------------------


@router.get(_COORDS + "/", response_model=PackageDetailResponse)
async def get_package(ecosystem: str, coordinates: str, db: AsyncSession = Depends(get_db_session)) -> PackageDetailResponse:
    target = await _target(db, ecosystem, coordinates)
    package = target.package
    rows = (
        await db.execute(
            select(Registry, PackageRegistry)
            .join(PackageRegistry, PackageRegistry.registry_id == Registry.id)
            .where(PackageRegistry.package_id == package.id)
            .order_by(Registry.priority, Registry.slug)
        )
    ).all()
    return PackageDetailResponse(
        ecosystem=package.ecosystem.value,
        name=package.name,
        normalized_name=package.normalized_name,
        namespace=package.namespace or "",
        purl=target.plugin.coordinates.to_purl(target.ref),
        origin=package.origin.value,
        description=package.description,
        homepage_url=package.homepage_url,
        repository_url=package.repository_url,
        documentation_url=package.documentation_url,
        license_raw=package.license_raw,
        license_spdx=package.license_spdx,
        latest_version=package.latest_version,
        synced_at=package.synced_at,
        unavailable_at=package.unavailable_at,
        sources=[
            PackageSourceItem(
                registry=registry.slug,
                enabled=registry.enabled,
                primary=str(registry.id) == str(package.primary_registry_id),
                first_seen_at=link.first_seen_at,
                checked_at=link.checked_at,
                missing_since=link.missing_since,
            )
            for registry, link in rows
        ],
    )


@router.get(_COORDS + "/versions", response_model=VersionListResponse)
async def list_versions(
    ecosystem: str,
    coordinates: str,
    db: AsyncSession = Depends(get_db_session),
    profile: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> VersionListResponse:
    """Versions the profile's sources publish (the default profile without `profile`), newest release first."""
    target = await _target(db, ecosystem, coordinates)
    registries = await visibility.visible_registries(db, target.plugin, target.ref, target.package.id,
                                                     await _profile(db, ecosystem, profile))
    cursor_payload = _decode_cursor_or_422(cursor)

    stmt = select(PackageVersion).where(PackageVersion.package_id == target.package.id, visibility.visible_clause(registries))
    stmt = stmt.order_by(PackageVersion.release_date.desc().nullslast(), PackageVersion.id.asc())

    if cursor_payload:
        rel = parse_iso_datetime(cursor_payload["release_date"]) if cursor_payload.get("release_date") else None
        id_value = parse_uuid(cursor_payload["id"])
        if rel is None:
            stmt = stmt.where(PackageVersion.release_date.is_(None), PackageVersion.id > id_value)
        else:
            stmt = stmt.where(
                or_(
                    PackageVersion.release_date < rel,
                    PackageVersion.release_date.is_(None),
                    and_(PackageVersion.release_date == rel, PackageVersion.id > id_value),
                )
            )

    rows = (await db.scalars(stmt.limit(limit + 1))).all()
    has_more = len(rows) > limit
    data_rows = rows[:limit]

    next_cursor = None
    if has_more and data_rows:
        last = data_rows[-1]
        next_cursor = encode_cursor(
            {
                "release_date": last.release_date.isoformat() if last.release_date else None,
                "id": str(last.id),
            }
        )

    statuses = await visibility.source_statuses(db, registries, [row.id for row in data_rows])
    data = [
        VersionItem(
            version=row.version,
            release_date=row.release_date,
            status=(visibility.effective_status(statuses.get(row.id, [])) or row.status).value,
            requires_python=_requires_python(row),
        )
        for row in data_rows
    ]
    return VersionListResponse(data=data, cursor=next_cursor, has_more=has_more)


@router.get(_COORDS + "/versions/{version}", response_model=VersionDetailResponse)
async def get_version(
    ecosystem: str, coordinates: str, version: str, db: AsyncSession = Depends(get_db_session)
) -> VersionDetailResponse:
    target = await _target(db, ecosystem, coordinates)
    row = await _version(db, target, version)
    source_rows = (
        await db.execute(
            select(Registry, VersionSource)
            .join(VersionSource, VersionSource.registry_id == Registry.id)
            .where(VersionSource.version_id == row.id)
            .order_by(Registry.priority, Registry.slug)
        )
    ).all()
    return VersionDetailResponse(
        name=target.package.name,
        purl=target.plugin.coordinates.to_purl(target.ref, row.version),
        version=row.version,
        release_date=row.release_date,
        status=row.status.value,
        requires_python=_requires_python(row),
        engines=row.engines,
        features=row.features,
        dependencies_state=row.dependencies_state.value,
        sources=[
            VersionSourceItem(registry=registry.slug, enabled=registry.enabled, status=link.status.value,
                              first_seen_at=link.first_seen_at)
            for registry, link in source_rows
        ],
    )


@router.get(_COORDS + "/versions/{version}/dependencies", response_model=DependencyTreeResponse)
async def get_dependencies_tree(
    ecosystem: str,
    coordinates: str,
    version: str,
    db: AsyncSession = Depends(get_db_session),
    profile: str | None = Query(default=None),
    depth: int | None = Query(default=None, ge=1, le=FULL_TREE_MAX_DEPTH),
    include_optional: bool = Query(default=False),
    python_version: str | None = Query(default=None),
    os: str | None = Query(default=None),
) -> DependencyTreeResponse:
    target = await _target(db, ecosystem, coordinates)
    version_row = await _version(db, target, version)
    view = _ProfileView(db, await _profile(db, ecosystem, profile))

    env = _build_marker_environment(python_version=python_version, os_name=os)
    fetcher = LazyFetcher(db=db)
    await ensure_version_dependencies(db, target.package, version_row)
    max_depth = depth or FULL_TREE_MAX_DEPTH
    nodes = await _build_tree_for_version(
        db=db,
        view=view,
        package=target.package,
        version_row=version_row,
        max_depth=max_depth,
        current_depth=1,
        include_optional=include_optional,
        expand_optional_at_current_level=True,
        env=env,
        visited={str(version_row.id)},
        fetcher=fetcher,
    )
    await db.commit()

    return DependencyTreeResponse(
        package=target.package.normalized_name,
        version=version,
        profile=view.profile.slug,
        depth=_measure_tree_depth(nodes),
        dependencies=nodes,
    )


@router.get(_COORDS + "/versions/{version}/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    ecosystem: str, coordinates: str, version: str, db: AsyncSession = Depends(get_db_session)
) -> ArtifactListResponse:
    target = await _target(db, ecosystem, coordinates)
    version_row = await _version(db, target, version)
    rows = (
        await db.execute(
            select(Artifact, Registry.slug)
            .join(Registry, Registry.id == Artifact.registry_id)
            .where(Artifact.version_id == version_row.id)
            .order_by(Artifact.filename.asc(), Registry.priority, Registry.slug)
        )
    ).all()

    return ArtifactListResponse(
        data=[
            ArtifactItem(
                filename=row.filename,
                artifact_type=row.artifact_type,
                download_url=row.download_url,
                size_bytes=row.size_bytes,
                checksum_sha256=row.checksum_sha256,
                checksum_md5=row.checksum_md5,
                registry=slug,
            )
            for row, slug in rows
        ]
    )


@router.get(_COORDS + "/vulnerabilities", response_model=VulnerabilityListResponse)
async def list_vulnerabilities(
    ecosystem: str, coordinates: str, db: AsyncSession = Depends(get_db_session)
) -> VulnerabilityListResponse:
    target = await _target(db, ecosystem, coordinates)
    rows = (
        await db.execute(
            select(Vulnerability)
            .join(PackageVulnerability, PackageVulnerability.vulnerability_id == Vulnerability.id)
            .where(PackageVulnerability.package_id == target.package.id)
            .order_by(Vulnerability.source_id.asc())
        )
    ).scalars().all()

    return VulnerabilityListResponse(
        package=target.package.normalized_name,
        data=[
            VulnerabilityItem(
                source=row.source.value,
                source_id=row.source_id,
                summary=row.summary,
                severity=row.severity.value if row.severity else None,
                fixed_in=row.fixed_in,
                link=row.link,
            )
            for row in rows
        ],
    )


# --- PyPI aliases (deprecated until E7) --------------------------------------------------------------------


@router.get("/packages/pypi/{name}", response_model=PackageDetailResponse, deprecated=True)
async def get_package_alias(name: str, db: AsyncSession = Depends(get_db_session)) -> PackageDetailResponse:
    return await get_package("pypi", name, db)


@router.get("/packages/pypi/{name}/versions", response_model=VersionListResponse, deprecated=True)
async def list_versions_alias(
    name: str,
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> VersionListResponse:
    return await list_versions("pypi", name, db, profile=None, limit=limit, cursor=cursor)


@router.get("/packages/pypi/{name}/versions/{ver}", response_model=VersionDetailResponse, deprecated=True)
async def get_version_alias(name: str, ver: str, db: AsyncSession = Depends(get_db_session)) -> VersionDetailResponse:
    return await get_version("pypi", name, ver, db)


@router.get("/packages/pypi/{name}/versions/{ver}/dependencies", response_model=DependencyTreeResponse, deprecated=True)
async def get_dependencies_tree_alias(
    name: str,
    ver: str,
    db: AsyncSession = Depends(get_db_session),
    depth: int | None = Query(default=None, ge=1, le=FULL_TREE_MAX_DEPTH),
    include_optional: bool = Query(default=False),
    python_version: str | None = Query(default=None),
    os: str | None = Query(default=None),
) -> DependencyTreeResponse:
    return await get_dependencies_tree("pypi", name, ver, db, profile=None, depth=depth,
                                       include_optional=include_optional, python_version=python_version, os=os)


@router.get("/packages/pypi/{name}/versions/{ver}/artifacts", response_model=ArtifactListResponse, deprecated=True)
async def list_artifacts_alias(name: str, ver: str, db: AsyncSession = Depends(get_db_session)) -> ArtifactListResponse:
    return await list_artifacts("pypi", name, ver, db)


@router.get("/packages/pypi/{name}/vulnerabilities", response_model=VulnerabilityListResponse, deprecated=True)
async def list_vulnerabilities_alias(name: str, db: AsyncSession = Depends(get_db_session)) -> VulnerabilityListResponse:
    return await list_vulnerabilities("pypi", name, db)


# --- lookups -----------------------------------------------------------------------------------------------


async def _target(db: AsyncSession, ecosystem: str, coordinates: str) -> _Target:
    """The catalog package at these coordinates, fetched on first request (lazy fetch); 404 if no source has it."""
    try:
        plugin = get_plugin(ecosystem)
    except UnknownEcosystem as exc:
        raise HTTPException(status_code=404, detail=f"Ecosystem '{ecosystem}' is not supported") from exc
    try:
        ref = plugin.coordinates.parse(coordinates)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Package '{coordinates}' not found: {exc}") from exc

    package = await find_package(db, ref)
    if package is None:
        await LazyFetcher(db=db).ensure_package(ref)
        package = await find_package(db, ref)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Package '{coordinates}' not found")
    return _Target(plugin, ref, package)


async def _profile(db: AsyncSession, ecosystem: str, slug: str | None) -> SourceProfile:
    profile = await sources.profile_by_slug(db, ecosystem, slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found for ecosystem '{ecosystem}'")
    return profile


async def _version(db: AsyncSession, target: _Target, version: str) -> PackageVersion:
    row = await db.scalar(
        select(PackageVersion).where(PackageVersion.package_id == target.package.id, PackageVersion.version == version)
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Version '{version}' not found")
    return row


def _requires_python(row: PackageVersion) -> str | None:
    """Alias of engines.python until E7 (IMPL T18)."""
    value = (row.engines or {}).get("python")
    return value if isinstance(value, str) else None


def _build_marker_environment(python_version: str | None, os_name: str | None) -> dict | None:
    if not python_version and not os_name:
        return None

    normalized_os = (os_name or "").lower()
    sys_platform = ""
    os_name_value = ""
    platform_system = ""
    if normalized_os in {"linux"}:
        sys_platform = "linux"
        os_name_value = "posix"
        platform_system = "Linux"
    elif normalized_os in {"windows", "win32"}:
        sys_platform = "win32"
        os_name_value = "nt"
        platform_system = "Windows"
    elif normalized_os in {"darwin", "mac", "macos"}:
        sys_platform = "darwin"
        os_name_value = "posix"
        platform_system = "Darwin"

    return {
        "python_version": python_version or "3.12",
        "python_full_version": f"{python_version or '3.12'}.0",
        "sys_platform": sys_platform,
        "os_name": os_name_value,
        "platform_machine": "x86_64",
        "platform_system": platform_system,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }


def _marker_applies(markers: dict | None, env: dict | None) -> bool:
    if markers is None:
        return True
    expression = markers.get("expression") if isinstance(markers, dict) else None
    if not expression:
        return True
    if env is None:
        return True
    try:
        return Marker(expression).evaluate(env)
    except Exception:  # noqa: BLE001
        return True


def _condition_applies(condition: dict[str, str], env: dict | None) -> bool:
    marker = condition.get("marker")
    op = condition.get("op")
    value = condition.get("value")
    if not marker or not op or value is None:
        return True
    if env is None:
        return True
    try:
        condition_expr = f"{marker} {op} {value!r}"
        return Marker(condition_expr).evaluate(env)
    except Exception:  # noqa: BLE001
        return True


def _tree_marker_applies(markers: dict | None, env: dict | None, include_optional: bool) -> bool:
    if not include_optional:
        return _marker_applies(markers, env)
    if markers is None or not isinstance(markers, dict):
        return True

    conditions = markers.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return _marker_applies(markers, env)

    non_extra_conditions = [
        condition
        for condition in conditions
        if isinstance(condition, dict) and condition.get("marker") != "extra"
    ]
    if not non_extra_conditions:
        return True

    return all(_condition_applies(condition, env) for condition in non_extra_conditions)


def _dependency_type_allowed(dep_type: DependencyType, include_optional: bool) -> bool:
    if dep_type == DependencyType.runtime:
        return True
    if dep_type == DependencyType.optional:
        return include_optional
    return False


def _has_extra_marker(markers: dict | None) -> bool:
    if not isinstance(markers, dict):
        return False
    conditions = markers.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(isinstance(condition, dict) and condition.get("marker") == "extra" for condition in conditions)


def _dependency_sort_key(dep: Dependency) -> tuple[int, int, str]:
    has_extra = bool(dep.extra_name) or _has_extra_marker(dep.environment_markers)
    dep_type_priority = 1 if dep.dependency_type == DependencyType.optional else 0
    extra_priority = 1 if has_extra else 0
    return (extra_priority, dep_type_priority, dep.dependency_name.lower())


@dataclass
class _ProfileView:
    """The tree through a profile: each package's visible sources, the dependency set its chosen source publishes,
    and level-1 links re-picked among visible versions when the stored link points at a hidden one."""

    db: AsyncSession
    profile: SourceProfile
    _registries: dict[object, list[RegistryConfig]] = field(default_factory=dict)
    _active: dict[object, list[tuple[str, object]]] = field(default_factory=dict)

    async def registries(self, package: Package) -> list[RegistryConfig]:
        if package.id not in self._registries:
            plugin = get_plugin(package.ecosystem.value)
            ref = PackageRef(ecosystem=package.ecosystem.value, namespace=package.namespace or "", name=package.normalized_name)
            self._registries[package.id] = await visibility.visible_registries(self.db, plugin, ref, package.id, self.profile)
        return self._registries[package.id]

    async def dependencies(self, package: Package, version_row: PackageVersion) -> list[Dependency]:
        """The canonical set, or the variant of the first visible source publishing the version if it has one."""
        registries = await self.registries(package)
        statuses = (await visibility.source_statuses(self.db, registries, [version_row.id])).get(version_row.id, [])
        slugs = {s.registry for s in statuses}
        chosen = next((r for r in registries if r.slug in slugs), None)
        rows = (
            await self.db.scalars(
                select(Dependency)
                .where(Dependency.version_id == version_row.id)
                .order_by(Dependency.dependency_type.asc(), Dependency.dependency_name.asc())
            )
        ).all()
        variant = [d for d in rows if chosen is not None and str(d.variant_registry_id) == chosen.id]
        return variant or [d for d in rows if d.variant_registry_id is None]

    async def pick(self, package: Package, constraint: str | None, linked_id: object | None) -> object | None:
        if package.id not in self._active:
            registries = await self.registries(package)
            rows = (
                await self.db.execute(
                    select(PackageVersion.version, PackageVersion.id)
                    .join(VersionSource, VersionSource.version_id == PackageVersion.id)
                    .where(
                        PackageVersion.package_id == package.id,
                        VersionSource.registry_id.in_([uuid.UUID(r.id) for r in registries]),
                        VersionSource.status == PackageVersionStatus.active,
                    )
                    .distinct()
                )
            ).all()
            self._active[package.id] = [(version, row_id) for version, row_id in rows]
        active = self._active[package.id]
        if linked_id is not None and any(row_id == linked_id for _, row_id in active):
            return linked_id
        return select_best_version(get_plugin(package.ecosystem.value), active, constraint)


async def _build_tree_for_version(
    db: AsyncSession,
    view: _ProfileView,
    package: Package,
    version_row: PackageVersion,
    max_depth: int,
    current_depth: int,
    include_optional: bool,
    expand_optional_at_current_level: bool,
    env: dict | None,
    visited: set[str],
    fetcher: LazyFetcher,
) -> list[DependencyNode]:
    await ensure_version_dependencies(db, package, version_row)
    deps = await view.dependencies(package, version_row)

    visible_deps: list[Dependency] = []
    for dep in deps:
        current_level_include_optional = include_optional and expand_optional_at_current_level

        if not _dependency_type_allowed(dep.dependency_type, include_optional=current_level_include_optional):
            continue
        if not _tree_marker_applies(dep.environment_markers, env, include_optional=current_level_include_optional):
            continue
        visible_deps.append(dep)

    nodes: list[DependencyNode] = []
    for dep in sorted(visible_deps, key=_dependency_sort_key):

        resolved_version = None
        children: list[DependencyNode] = []
        cycle = False

        if not dep.resolved_package_id:
            await _ensure_dependency_resolved(db, dep, fetcher)
        child_package = (
            await db.scalar(select(Package).where(Package.id == dep.resolved_package_id)) if dep.resolved_package_id else None
        )
        resolved_version_id = await view.pick(child_package, dep.version_constraint, dep.resolved_version_id) if child_package else None

        if child_package and resolved_version_id:
            resolved = await db.scalar(select(PackageVersion).where(PackageVersion.id == resolved_version_id))
            if resolved:
                resolved_version = resolved.version
                resolved_key = str(resolved.id)
                if current_depth < max_depth:
                    if resolved_key in visited:
                        cycle = True
                    else:
                        await ensure_version_dependencies(db, child_package, resolved)
                        children = await _build_tree_for_version(
                            db=db,
                            view=view,
                            package=child_package,
                            version_row=resolved,
                            max_depth=max_depth,
                            current_depth=current_depth + 1,
                            include_optional=include_optional,
                            expand_optional_at_current_level=False,
                            env=env,
                            visited=visited | {resolved_key},
                            fetcher=fetcher,
                        )

        nodes.append(
            DependencyNode(
                name=dep.dependency_name,
                constraint=dep.version_constraint,
                dependency_type=dep.dependency_type.value,
                extra_name=dep.extra_name,
                environment_markers=dep.environment_markers,
                resolved_version=resolved_version,
                cycle=cycle,
                children=children,
            )
        )

    return nodes


async def _ensure_dependency_resolved(db: AsyncSession, dep: Dependency, fetcher: LazyFetcher) -> None:
    package = await fetcher.ensure_package(
        PackageRef(ecosystem=dep.dependency_ecosystem.value, namespace=dep.dependency_namespace or "", name=dep.dependency_name)
    )
    if not package:
        return
    await resolve_dependency_links(db, [dep.version_id])
    await db.flush()
    await db.refresh(dep)


def _measure_tree_depth(nodes: list[DependencyNode]) -> int:
    if not nodes:
        return 0
    return 1 + max(_measure_tree_depth(node.children) for node in nodes)
