from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from packaging.markers import Marker
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.packages import _decode_cursor_or_422
from app.db import get_db_session
from app.ecosystems.base import PackageRef
from app.fetcher.lazy import LazyFetcher
from app.fetcher.pipeline import ensure_version_dependencies, resolve_dependency_links
from app.models.artifact import Artifact
from app.models.dependency import Dependency
from app.models.enums import DependencyType, Ecosystem
from app.models.package import Package
from app.models.version import PackageVersion
from app.models.vulnerability import PackageVulnerability, Vulnerability
from app.normalizers.names import normalize_package_name
from app.schemas.packages import (
    ArtifactItem,
    ArtifactListResponse,
    DependencyNode,
    DependencyTreeResponse,
    PackageDetailResponse,
    VersionDetailResponse,
    VersionItem,
    VersionListResponse,
    VulnerabilityItem,
    VulnerabilityListResponse,
)
from app.services.cursor import encode_cursor, parse_iso_datetime, parse_uuid

router = APIRouter()
FULL_TREE_MAX_DEPTH = 200

@router.get("/packages/pypi/{name}", response_model=PackageDetailResponse)
async def get_package(name: str, db: AsyncSession = Depends(get_db_session)) -> PackageDetailResponse:
    package = await _find_or_fetch_package(db, name)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package '{name}' not found")

    return PackageDetailResponse(
        ecosystem=package.ecosystem.value,
        name=package.name,
        normalized_name=package.normalized_name,
        description=package.description,
        homepage_url=package.homepage_url,
        repository_url=package.repository_url,
        documentation_url=package.documentation_url,
        license_raw=package.license_raw,
        license_spdx=package.license_spdx,
        latest_version=package.latest_version,
        synced_at=package.synced_at,
    )


@router.get("/packages/pypi/{name}/versions", response_model=VersionListResponse)
async def list_versions(
    name: str,
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> VersionListResponse:
    package = await _find_or_fetch_package(db, name)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package '{name}' not found")

    cursor_payload = _decode_cursor_or_422(cursor)

    stmt = select(PackageVersion).where(PackageVersion.package_id == package.id)
    stmt = stmt.order_by(PackageVersion.release_date.desc().nullslast(), PackageVersion.id.asc())

    if cursor_payload:
        rel = parse_iso_datetime(cursor_payload["release_date"]) if cursor_payload.get("release_date") else None
        id_value = parse_uuid(cursor_payload["id"])
        if rel is None:
            stmt = stmt.where(PackageVersion.id > id_value)
        else:
            stmt = stmt.where(
                or_(
                    PackageVersion.release_date < rel,
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

    data = [
        VersionItem(
            version=row.version,
            release_date=row.release_date,
            status=row.status.value,
            requires_python=row.requires_python,
        )
        for row in data_rows
    ]
    return VersionListResponse(data=data, cursor=next_cursor, has_more=has_more)


@router.get("/packages/pypi/{name}/versions/{ver}", response_model=VersionDetailResponse)
async def get_version(name: str, ver: str, db: AsyncSession = Depends(get_db_session)) -> VersionDetailResponse:
    package = await _find_or_fetch_package(db, name)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package '{name}' not found")

    row = await db.scalar(select(PackageVersion).where(PackageVersion.package_id == package.id, PackageVersion.version == ver))
    if not row:
        raise HTTPException(status_code=404, detail=f"Version '{ver}' not found")

    return VersionDetailResponse(
        name=package.name,
        version=row.version,
        release_date=row.release_date,
        status=row.status.value,
        requires_python=row.requires_python,
        features=row.features,
    )


@router.get(
    "/packages/pypi/{name}/versions/{ver}/dependencies",
    response_model=DependencyTreeResponse,
)
async def get_dependencies_tree(
    name: str,
    ver: str,
    db: AsyncSession = Depends(get_db_session),
    depth: int | None = Query(default=None, ge=1, le=FULL_TREE_MAX_DEPTH),
    include_optional: bool = Query(default=False),
    python_version: str | None = Query(default=None),
    os: str | None = Query(default=None),
) -> DependencyTreeResponse:
    package = await _find_or_fetch_package(db, name)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package '{name}' not found")

    version_row = await db.scalar(select(PackageVersion).where(PackageVersion.package_id == package.id, PackageVersion.version == ver))
    if not version_row:
        raise HTTPException(status_code=404, detail=f"Version '{ver}' not found")

    env = _build_marker_environment(python_version=python_version, os_name=os)
    fetcher = LazyFetcher(db=db)
    await ensure_version_dependencies(db, package, version_row)
    max_depth = depth or FULL_TREE_MAX_DEPTH
    nodes = await _build_tree_for_version(
        db=db,
        package=package,
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
        package=package.normalized_name,
        version=ver,
        depth=_measure_tree_depth(nodes),
        dependencies=nodes,
    )


@router.get(
    "/packages/pypi/{name}/versions/{ver}/artifacts",
    response_model=ArtifactListResponse,
)
async def list_artifacts(name: str, ver: str, db: AsyncSession = Depends(get_db_session)) -> ArtifactListResponse:
    package = await _find_or_fetch_package(db, name)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package '{name}' not found")

    version_row = await db.scalar(select(PackageVersion).where(PackageVersion.package_id == package.id, PackageVersion.version == ver))
    if not version_row:
        raise HTTPException(status_code=404, detail=f"Version '{ver}' not found")

    artifacts = (await db.scalars(select(Artifact).where(Artifact.version_id == version_row.id).order_by(Artifact.filename.asc()))).all()

    return ArtifactListResponse(
        data=[
            ArtifactItem(
                filename=row.filename,
                artifact_type=row.artifact_type,
                download_url=row.download_url,
                size_bytes=row.size_bytes,
                checksum_sha256=row.checksum_sha256,
                checksum_md5=row.checksum_md5,
            )
            for row in artifacts
        ]
    )


@router.get("/packages/pypi/{name}/vulnerabilities", response_model=VulnerabilityListResponse)
async def list_vulnerabilities(name: str, db: AsyncSession = Depends(get_db_session)) -> VulnerabilityListResponse:
    package = await _find_or_fetch_package(db, name)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package '{name}' not found")

    rows = (
        await db.execute(
            select(Vulnerability)
            .join(PackageVulnerability, PackageVulnerability.vulnerability_id == Vulnerability.id)
            .where(PackageVulnerability.package_id == package.id)
            .order_by(Vulnerability.source_id.asc())
        )
    ).scalars().all()

    return VulnerabilityListResponse(
        package=package.normalized_name,
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


async def _find_or_fetch_package(db: AsyncSession, name: str) -> Package | None:
    normalized = normalize_package_name(name)
    package = await db.scalar(
        select(Package).where(Package.ecosystem == Ecosystem.pypi, Package.normalized_name == normalized)
    )
    if package:
        return package

    await LazyFetcher(db=db).ensure_package(PackageRef(ecosystem="pypi", namespace="", name=normalized))

    return await db.scalar(
        select(Package).where(Package.ecosystem == Ecosystem.pypi, Package.normalized_name == normalized)
    )


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


async def _build_tree_for_version(
    db: AsyncSession,
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
    deps = (
        await db.scalars(
            select(Dependency)
            # Canonical set only; per-source variants are chosen by profile with the graph format (E0-C).
            .where(Dependency.version_id == version_row.id, Dependency.variant_registry_id.is_(None))
            .order_by(Dependency.dependency_type.asc(), Dependency.dependency_name.asc())
        )
    ).all()

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

        resolved_version_id = dep.resolved_version_id
        if not resolved_version_id:
            resolved_version_id = await _ensure_dependency_resolved(db, dep, fetcher)

        if resolved_version_id:
            resolved = await db.scalar(select(PackageVersion).where(PackageVersion.id == resolved_version_id))
            if resolved:
                resolved_version = resolved.version
                resolved_key = str(resolved.id)
                if current_depth < max_depth:
                    if resolved_key in visited:
                        cycle = True
                    else:
                        child_package = await db.scalar(select(Package).where(Package.id == resolved.package_id))
                        if child_package:
                            await ensure_version_dependencies(db, child_package, resolved)
                            children = await _build_tree_for_version(
                                db=db,
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


async def _ensure_dependency_resolved(db: AsyncSession, dep: Dependency, fetcher: LazyFetcher) -> str | None:
    package = await fetcher.ensure_package(
        PackageRef(ecosystem=dep.dependency_ecosystem.value, namespace=dep.dependency_namespace or "", name=dep.dependency_name)
    )
    if not package:
        return None

    await resolve_dependency_links(db, [dep.version_id])
    await db.flush()
    refreshed = await db.scalar(select(Dependency).where(Dependency.id == dep.id))
    return refreshed.resolved_version_id if refreshed else None


def _measure_tree_depth(nodes: list[DependencyNode]) -> int:
    if not nodes:
        return 0
    return 1 + max(_measure_tree_depth(node.children) for node in nodes)
