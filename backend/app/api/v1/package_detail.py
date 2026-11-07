"""One package: purl addressing `/packages/{ecosystem}/{coordinates}/-/…` (SPEC-MULTI-ECOSYSTEM.md §11.1) and the
PyPI routes `/packages/pypi/{name}/…`, which stay as deprecated aliases until the UI moves (E7)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from packaging.markers import Marker
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.packages import _decode_cursor_or_422
from app.db import get_db_session
from app.ecosystems import purl as purls
from app.ecosystems.base import EcosystemPlugin, Environment, PackageRef, RegistryConfig, SourceProfile
from app.ecosystems.crates.environment import CratesEnvironment
from app.ecosystems.crates.resolver import enabled as crate_features_enabled
from app.ecosystems.npm.environment import NpmEnvironment
from app.ecosystems.npm.environment import version_allows as npm_version_allows
from app.ecosystems.registry import UnknownEcosystem, get_plugin
from app.fetcher import sources
from app.fetcher.lazy import LazyFetcher
from app.fetcher.pipeline import (
    UNAVAILABLE_DIGEST,
    choose_version,
    dependency_from_row,
    find_package,
    source_dependencies,
)
from app.models.artifact import Artifact
from app.models.dependency import Dependency
from app.models.enums import DependenciesState, DependencyType, PackageVersionStatus
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
    DependentItem,
    DependentListResponse,
    DependentTotals,
    OverviewDependencies,
    OverviewLicense,
    OverviewLine,
    OverviewMonth,
    OverviewUsedBy,
    OverviewVersions,
    PackageDetailResponse,
    PackageOverviewResponse,
    PackageSourceItem,
    VersionDetailResponse,
    VersionItem,
    VersionListResponse,
    VersionSourceItem,
    VulnerabilityItem,
    VulnerabilityListResponse,
)
from app.services import release_lines, usage, visibility
from app.services.cursor import CursorError, encode_cursor, parse_iso_datetime, parse_uuid
from app.services.purls import package_purl

router = APIRouter()
logger = logging.getLogger(__name__)
FULL_TREE_MAX_DEPTH = 200
API_PREFIX = "/api/v1"
_COORDS = "/packages/{ecosystem}/{coordinates:path}/-"
# Only the PyPI connector stores advisories; NuGet receives them and drops them (D6), the other ecosystems have no
# source. For the rest an empty list must not read as "checked and clean" (#68).
_ADVISORY_ECOSYSTEMS = frozenset({"pypi"})


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
async def by_purl(purl: str, request: Request) -> RedirectResponse:
    """Package or version by purl: a redirect to its canonical path. Qualifiers are ignored, except where they name
    the package (rpm `distro`)."""
    # `?distro=rocky-9` written as is reaches the route as the query string, not as part of the path.
    if request.url.query:
        purl = f"{purl}?{request.url.query}"
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
        deprecation=package.deprecation,
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


def _versions_cursor_or_422(cursor: str | None) -> tuple[datetime | None, uuid.UUID] | None:
    """The (release_date, id) a versions cursor points after; 422 for a cursor whose payload is not that."""
    payload = _decode_cursor_or_422(cursor)
    if not payload:
        return None
    try:
        raw_date = payload.get("release_date")
        if raw_date is not None and not isinstance(raw_date, str):
            raise TypeError("release_date")
        rel = parse_iso_datetime(raw_date) if raw_date else None
        return rel, parse_uuid(payload["id"])
    except CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor payload") from exc


def _not_active_clause(registries: Sequence[RegistryConfig]) -> ColumnElement[bool]:
    """Effective status is not `active`: the status a reader sees is the best one among the visible sources
    (`visibility.effective_status`), and `active` is the best, so a version is not active exactly when none of its
    visible sources says `active`. The stored `PackageVersion.status` is the merged view and may differ."""
    return ~exists(
        select(VersionSource.version_id).where(
            VersionSource.version_id == PackageVersion.id,
            VersionSource.registry_id.in_([uuid.UUID(r.id) for r in registries]),
            VersionSource.status == PackageVersionStatus.active,
        )
    )


@router.get(_COORDS + "/versions", response_model=VersionListResponse)
async def list_versions(
    ecosystem: str,
    coordinates: str,
    db: AsyncSession = Depends(get_db_session),
    profile: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    prerelease: bool | None = Query(default=None),
    not_active: bool | None = Query(default=None),
    line: str | None = Query(default=None),
) -> VersionListResponse:
    """Versions the profile's sources publish (the default profile without `profile`), newest release first.

    `prerelease=true|false` keeps only prereleases or only stable versions; `not_active=true` keeps versions whose
    effective status (the one the rows show) is not `active`, `false` the active ones. Both are SQL filters and work
    with the cursor.

    `line` (`2.14`, `v1`) cannot be expressed in SQL for every ecosystem (the line is derived from the version string,
    see `release_lines.line_of`), so it is applied in Python over the visible rows in the usual order: stable and
    prerelease versions both, unless `prerelease` narrows it. A line is small, so all its versions come back at once:
    `limit` and `cursor` are ignored, `cursor` is null and `has_more` false."""
    position = _versions_cursor_or_422(cursor)
    target = await _target(db, ecosystem, coordinates)
    registries = await visibility.visible_registries(db, target.plugin, target.ref, target.package.id,
                                                     await _profile(db, ecosystem, profile))

    stmt = select(PackageVersion).where(PackageVersion.package_id == target.package.id, visibility.visible_clause(registries))
    if prerelease is not None:
        stmt = stmt.where(PackageVersion.is_prerelease.is_(prerelease))
    if not_active is not None:
        clause = _not_active_clause(registries)
        stmt = stmt.where(clause if not_active else ~clause)
    stmt = stmt.order_by(PackageVersion.release_date.desc().nullslast(), PackageVersion.id.asc())

    if line is not None:
        in_line = release_lines.filter_line(target.plugin.ecosystem, (await db.scalars(stmt)).all(), line)
        return VersionListResponse(data=await _version_items(db, registries, target.plugin.ecosystem, in_line),
                                   cursor=None, has_more=False)

    if position:
        rel, id_value = position
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

    data = await _version_items(db, registries, target.plugin.ecosystem, data_rows)
    return VersionListResponse(data=data, cursor=next_cursor, has_more=has_more)


async def _version_items(db: AsyncSession, registries: Sequence[RegistryConfig], ecosystem: str,
                         rows: Sequence[PackageVersion]) -> list[VersionItem]:
    statuses = await visibility.source_statuses(db, registries, [row.id for row in rows])
    return [
        VersionItem(
            version=row.version,
            release_date=row.release_date,
            status=(visibility.effective_status(statuses.get(row.id, [])) or row.status).value,
            status_reason=row.status_reason,
            deprecation=row.deprecation,
            requires_python=_requires_python(row),
            line=release_lines.line_of(ecosystem, row.version),
            is_prerelease=row.is_prerelease,
        )
        for row in rows
    ]


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
        status_reason=row.status_reason,
        deprecation=row.deprecation,
        requires_python=_requires_python(row),
        line=release_lines.line_of(target.plugin.ecosystem, row.version),
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
    node_version: str | None = Query(default=None),
    cpu: str | None = Query(default=None),
    target_triple: str | None = Query(default=None, alias="target"),
    features: str | None = Query(default=None),
    default_features: bool | None = Query(default=None),
    target_framework: str | None = Query(default=None),
    scopes: str | None = Query(default=None),
    arch: str | None = Query(default=None),
) -> DependencyTreeResponse:
    """The tree in the ecosystem's environment: PyPI `python_version`/`os` (markers), npm `node_version`/`os`/`cpu`
    (optional dependencies whose version does not fit the platform are left out, as npm does), crates `target`,
    `features` (comma-separated) and `default_features` (below the root, each edge asks its own features), NuGet
    `target_framework` (each version's nearest dependency group), Maven `scopes` (comma-separated, by default
    compile and runtime), RPM `arch` (x86_64 by default; a capability's package comes from rpm-worker)."""
    target = await _target(db, ecosystem, coordinates)
    version_row = await _version(db, target, version)
    view = _ProfileView(db, await _profile(db, ecosystem, profile))

    env = None
    if target.plugin.ecosystem == "npm":
        npm_env = target.plugin.parse_environment({"node_version": node_version, "os": os, "cpu": cpu})
        view.plugin_env = npm_env if isinstance(npm_env, NpmEnvironment) else NpmEnvironment()
        view.plugin = target.plugin
    elif target.plugin.ecosystem == "nuget":
        try:
            view.plugin_env = target.plugin.parse_environment({"target_framework": target_framework or "net8.0"})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view.plugin = target.plugin
    elif target.plugin.ecosystem == "rpm":
        try:
            view.plugin_env = target.plugin.parse_environment({"arch": arch or "x86_64"})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view.plugin = target.plugin
    elif target.plugin.ecosystem == "maven":
        try:
            view.plugin_env = target.plugin.parse_environment({"scopes": scopes or "compile,runtime"})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view.plugin = target.plugin
    elif target.plugin.ecosystem == "crates":
        try:
            view.plugin_env = target.plugin.parse_environment(
                {"target": target_triple, "features": features, "default_features": default_features})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view.plugin = target.plugin
    else:
        env = _build_marker_environment(python_version=python_version, os_name=os)
    fetcher = LazyFetcher(db=db)
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
        package=target.display,
        version=version,
        profile=view.profile.slug,
        depth=_measure_tree_depth(nodes),
        dependencies_unknown=version_row.id in view.unknown,
        dependencies=nodes,
    )


@router.get(_COORDS + "/overview", response_model=PackageOverviewResponse)
async def get_overview(
    ecosystem: str,
    coordinates: str,
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> PackageOverviewResponse:
    """What the package card's Overview shows, for `version` (the latest by default): dependency, dependent and
    license counts over the catalog's graph (latest versions, `used_by` types), release activity and release lines.
    Counts are for this catalog, not the registry: indirect ones are a lower bound."""
    target = await _target(db, ecosystem, coordinates)
    package = target.package
    profile = await _profile(db, ecosystem, None)
    registries = await visibility.visible_registries(db, target.plugin, target.ref, package.id, profile)
    visible = visibility.visible_clause(registries)

    version_row = await _version(db, target, version) if version else None
    if version_row is None and version is None and package.latest_version:
        version_row = await _version(db, target, package.latest_version)

    dependencies = OverviewDependencies(direct=None, optional=None, dev=None, indirect=None, not_in_catalog=None)
    licenses: list[tuple[str, int]] = []
    if version_row is not None:
        # The tree route's way: the version's dependencies are loaded on first read; unknown ones stay null.
        view = _ProfileView(db, profile)
        rows = await view.dependencies(package, version_row)
        edges = [((dep.dependency_ecosystem.value, dep.dependency_namespace or "", dep.dependency_name),
                  dep.dependency_type, dep.resolved_package_id) for dep in rows]
        unknown = version_row.id in view.unknown
        await db.commit()
        if not unknown:
            summary = usage.summarize_dependencies(edges)
            reached = await usage.reachable(db, summary.direct_ids, forward=True)
            indirect = reached - summary.direct_ids - {package.id}
            missing = summary.not_in_catalog | await usage.unresolved_edges(db, reached - {package.id})
            dependencies = OverviewDependencies(direct=summary.direct, optional=summary.optional, dev=summary.dev,
                                                indirect=len(indirect), not_in_catalog=len(missing))
            licenses = await usage.license_counts(db, reached - {package.id})

    used_by_direct = (await usage.fetch_used_by(db, [package.id])).get(package.id, 0)
    dependents = await usage.reachable(db, [package.id], forward=False)
    used_by = OverviewUsedBy(direct=used_by_direct, indirect=max(len(dependents - {package.id}) - used_by_direct, 0))

    now = datetime.now(UTC)
    months = release_lines.last_months(now)
    month_of = func.to_char(func.timezone("UTC", PackageVersion.release_date), "YYYY-MM")
    in_package = (PackageVersion.package_id == package.id, visible)
    total, prereleases, not_active = (await db.execute(
        select(
            func.count(),
            func.count().filter(PackageVersion.is_prerelease.is_(True)),
            func.count().filter(_not_active_clause(registries)),
        ).where(*in_package)
    )).one()
    month_counts: dict[str, int] = dict((await db.execute(
        select(month_of, func.count()).where(*in_package, month_of.in_(months)).group_by(month_of)
    )).all())
    stable = (await db.execute(
        select(PackageVersion.version, PackageVersion.release_date, PackageVersion.sort_key).where(
            *in_package, PackageVersion.is_prerelease.is_(False), PackageVersion.release_date.is_not(None))
    )).all()
    lines = release_lines.build_lines(
        target.plugin.ecosystem,
        (release_lines.ReleasedVersion(name, released_at, sort_key)
         for name, released_at, sort_key in stable if released_at is not None),
    )

    return PackageOverviewResponse(
        version=version_row.version if version_row is not None else None,
        dependencies=dependencies,
        used_by=used_by,
        licenses=[OverviewLicense(license=name, count=count) for name, count in licenses],
        versions=OverviewVersions(total=total, prereleases=prereleases, not_active=not_active),
        activity=[OverviewMonth(**month) for month in release_lines.fill_months(month_counts, now)],
        lines=[OverviewLine(**asdict(line)) for line in lines],
    )


def _dependents_cursor_or_422(cursor: str | None) -> tuple[bool, str, uuid.UUID] | None:
    """The (indirect, normalized_name, id) a dependents cursor points after; 422 for a cursor that is not one."""
    payload = _decode_cursor_or_422(cursor)
    if not payload:
        return None
    try:
        indirect, name = payload["indirect"], payload["name"]
        if not isinstance(indirect, bool) or not isinstance(name, str):
            raise TypeError("cursor")
        return indirect, name, parse_uuid(payload["id"])
    except CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor payload") from exc


@router.get(_COORDS + "/dependents", response_model=DependentListResponse)
async def list_dependents(
    ecosystem: str,
    coordinates: str,
    relation: Literal["direct", "indirect"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> DependentListResponse:
    """The catalog packages that break if this one changes, direct first: the same graph as `used_by` (latest
    versions, runtime/peer/provided, no self-edges). `totals` are those of the overview, whatever `relation` is."""
    after = _dependents_cursor_or_422(cursor)
    target = await _target(db, ecosystem, coordinates)
    page = await usage.fetch_dependents(db, target.package.id, relation=relation, limit=limit, after=after)

    next_cursor = None
    if page.has_more and page.rows:
        last = page.rows[-1]
        next_cursor = encode_cursor({"indirect": not last.direct, "name": last.package.normalized_name,
                                     "id": str(last.package.id)})
    return DependentListResponse(
        totals=DependentTotals(direct=page.direct_total, indirect=page.indirect_total),
        data=[
            DependentItem(
                ecosystem=row.package.ecosystem.value,
                namespace=row.package.namespace or "",
                name=row.package.name,
                normalized_name=row.package.normalized_name,
                purl=package_purl(row.package.ecosystem.value, row.package.namespace, row.package.normalized_name),
                latest_version=row.package.latest_version,
                relation="direct" if row.direct else "indirect",
                dependency_types=[t.value for t in row.dependency_types],
                requires=list(row.requires),
            )
            for row in page.rows
        ],
        cursor=next_cursor,
        has_more=page.has_more,
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
                arch=row.arch,
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
        package=target.display,
        coverage="collected" if target.ref.ecosystem in _ADVISORY_ECOSYSTEMS else "not_collected",
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
    return await list_versions("pypi", name, db, profile=None, limit=limit, cursor=cursor, prerelease=None,
                               not_active=None, line=None)


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
                                       include_optional=include_optional, python_version=python_version, os=os,
                                       node_version=None, cpu=None)


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
    unknown: set[object] = field(default_factory=set)   # versions whose dependencies are unknown
    plugin_env: Environment | None = None                 # npm, crates: the plugin's rules instead of PEP 508 markers
    plugin: EcosystemPlugin | None = None                 # with plugin_env: decides which dependencies apply
    # RPM: capability -> (provider package id, build version id) for this view's arch and profile
    providers: dict[tuple[str, str], tuple[object, object | None] | None] = field(default_factory=dict)

    async def registries(self, package: Package) -> list[RegistryConfig]:
        if package.id not in self._registries:
            plugin = get_plugin(package.ecosystem.value)
            ref = PackageRef(ecosystem=package.ecosystem.value, namespace=package.namespace or "", name=package.normalized_name)
            self._registries[package.id] = await visibility.visible_registries(self.db, plugin, ref, package.id, self.profile)
        return self._registries[package.id]

    async def dependencies(self, package: Package, version_row: PackageVersion) -> list[Dependency]:
        """As published by the source the version is taken from (visibility.chosen_source); [] when unknown."""
        registries = await self.registries(package)
        statuses = (await visibility.source_statuses(self.db, registries, [version_row.id])).get(version_row.id, [])
        slug = visibility.chosen_source(statuses)
        chosen = next((r for r in registries if r.slug == slug), None)
        registry_id = chosen.id if chosen is not None else str(version_row.metadata_registry_id)
        try:
            async with self.db.begin_nested():
                rows, _ = await source_dependencies(self.db, package, version_row, registry_id, self.profile)
        except Exception:  # one source failing leaves that node unknown, not the whole tree a 500
            logger.warning("Dependencies of %s %s could not be loaded", package.normalized_name, version_row.version,
                           exc_info=True)
            rows = None
        if rows is None:
            self.unknown.add(version_row.id)
        return list(rows or [])

    async def known_unknown(self, package: Package, version_row: PackageVersion) -> bool:
        """For a node the tree does not expand: are its dependencies already known to be unknown? Nothing is loaded."""
        registries = await self.registries(package)
        statuses = (await visibility.source_statuses(self.db, registries, [version_row.id])).get(version_row.id, [])
        slug = visibility.chosen_source(statuses)
        chosen = next((r for r in registries if r.slug == slug), None)
        if chosen is None or chosen.id == str(version_row.metadata_registry_id):
            return version_row.dependencies_state == DependenciesState.unavailable
        digest = await self.db.scalar(select(VersionSource.metadata_digest).where(
            VersionSource.version_id == version_row.id, VersionSource.registry_id == uuid.UUID(chosen.id)))
        return digest == UNAVAILABLE_DIGEST

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
        return await choose_version(self.db, get_plugin(package.ecosystem.value), package.id, active, constraint,
                                    self.plugin_env)


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
    plugin_env: Environment | None = None,
) -> list[DependencyNode]:
    deps = await view.dependencies(package, version_row)
    # The rows are only read from here on, so they leave the session (#49): a lazy fetch below that fails against a
    # sync of the same packages rolls back its savepoint, which expires the rows it changed (`_link_dependents` links
    # these very ones), and an expired row cannot be loaded again in async code.
    for dep in deps:
        if dep in db:
            db.expunge(dep)
    plugin_env = plugin_env or view.plugin_env
    asks = _crate_asks(plugin_env, version_row, deps)

    visible_deps: list[Dependency] = []
    for dep in deps:
        current_level_include_optional = include_optional and expand_optional_at_current_level

        if plugin_env is not None and view.plugin is not None:
            # npm installs optional dependencies at every level and (npm >= 7) peers that are not optional;
            # crates: optional ones come with the features asked of this crate.
            if not view.plugin.dependency_applies(dependency_from_row(dep), plugin_env, include_optional):
                continue
        elif not _dependency_type_allowed(dep.dependency_type, include_optional=current_level_include_optional):
            continue
        elif not _tree_marker_applies(dep.environment_markers, env, include_optional=current_level_include_optional):
            continue
        visible_deps.append(dep)

    nodes: list[DependencyNode] = []
    for dep in sorted(visible_deps, key=_dependency_sort_key):

        resolved_version = None
        children: list[DependencyNode] = []
        cycle = False
        dependencies_unknown = False

        linked: tuple[object, object | None] | None
        if dep.dependency_ecosystem.value == "rpm" and view.plugin_env is not None:
            linked = await _rpm_provider(db, dep, fetcher, view)
        elif dep.resolved_package_id:
            linked = (dep.resolved_package_id, dep.resolved_version_id)
        else:
            found = await _ensure_dependency_resolved(dep, fetcher)
            linked = (found, None) if found is not None else None    # the version is picked below
        child_package = await db.scalar(select(Package).where(Package.id == linked[0])) if linked else None
        # An RPM constraint is on the capability (`python(abi) = 3.9`), not on the provider's versions.
        constraint = None if dep.dependency_ecosystem.value == "rpm" else dep.version_constraint
        resolved_version_id = await view.pick(child_package, constraint, linked[1]) if child_package and linked else None

        if child_package and resolved_version_id:
            resolved = await db.scalar(select(PackageVersion).where(PackageVersion.id == resolved_version_id))
            if resolved and isinstance(plugin_env, NpmEnvironment) and dep.dependency_type == DependencyType.optional \
                    and not npm_version_allows(resolved.engines, plugin_env):
                continue   # not for this platform: npm skips it without an error
            if resolved:
                resolved_version = resolved.version
                resolved_key = str(resolved.id)
                if current_depth < max_depth:
                    if resolved_key in visited:
                        cycle = True
                    else:
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
                            plugin_env=_child_env(plugin_env, dep, asks),
                        )
                dependencies_unknown = resolved.id in view.unknown or await view.known_unknown(child_package, resolved)

        name = _dependency_display(dep)
        resolved_name = None
        if child_package is not None:
            shown = get_plugin(child_package.ecosystem.value).coordinates.display(PackageRef(
                ecosystem=child_package.ecosystem.value, namespace=child_package.namespace or "",
                name=child_package.normalized_name))
            resolved_name = shown if shown != name else None
        nodes.append(
            DependencyNode(
                name=name,
                resolved_name=resolved_name,
                constraint=dep.version_constraint,
                dependency_type=dep.dependency_type.value,
                extra_name=dep.extra_name,
                scope=dep.scope,
                environment_markers=dep.environment_markers,
                details=dep.details,
                resolved_version=resolved_version,
                cycle=cycle,
                dependencies_unknown=dependencies_unknown,
                children=children,
            )
        )

    return nodes


def _crate_asks(env: Environment | None, version_row: PackageVersion, deps: list[Dependency]) -> dict[str, frozenset[str]]:
    """crates: the features this version's own active features ask of its dependencies (`x/feat`), by local name."""
    if not isinstance(env, CratesEnvironment):
        return {}
    features = {name: [str(v) for v in values] for name, values in (version_row.features or {}).items()
                if isinstance(values, list)}
    switched = crate_features_enabled(features, [dependency_from_row(dep) for dep in deps], env.active_features())
    return dict(switched.asks)


def _child_env(env: Environment | None, dep: Dependency, asks: dict[str, frozenset[str]]) -> Environment | None:
    """crates: below an edge, the features that edge asks of its crate (and its default features unless switched off)
    and those the parent's features ask of it."""
    if not isinstance(env, CratesEnvironment):
        return env
    details = dep.details or {}
    listed = details.get("features")
    features = {str(f) for f in listed} if isinstance(listed, list) else set()
    features |= asks.get(dep.alias or dep.dependency_name, frozenset())
    return CratesEnvironment(target=env.target, features=tuple(sorted(features)),
                             default_features=details.get("default_features", True) is not False)


def _dependency_ref(dep: Dependency) -> PackageRef:
    return PackageRef(ecosystem=dep.dependency_ecosystem.value, namespace=dep.dependency_namespace or "",
                      name=dep.dependency_name)


def _dependency_display(dep: Dependency) -> str:
    """The name as the ecosystem writes it: npm `@scope/name`, Maven `group:artifact` (the row keeps the parts); an RPM
    capability as written (`libssl.so.3()(64bit)`), its package being `resolved_name`."""
    ref = _dependency_ref(dep)
    if ref.ecosystem == "rpm":
        return dep.dependency_name
    try:
        return get_plugin(ref.ecosystem).coordinates.display(ref)
    except UnknownEcosystem:
        return dep.dependency_name


async def _rpm_provider(db: AsyncSession, dep: Dependency, fetcher: LazyFetcher,
                        view: _ProfileView) -> tuple[object, object | None] | None:
    """An RPM dependency names a capability: rpm-worker knows which package (and build) provides it in the view's
    arch and profile (SPEC §2.2). Asked per tree and kept for it only: the catalog row is shared by every arch and
    profile, and the newest providing build changes with the repositories. None: no provider."""
    find = getattr(view.plugin, "provider", None)
    if find is None or view.plugin_env is None:
        return None
    key = (dep.dependency_namespace or "", dep.raw_spec or dep.dependency_name)
    if key not in view.providers:
        view.providers[key] = None
        try:
            found = await find(dependency_from_row(dep), view.plugin_env, view.profile)
        except Exception:  # the worker or a source failing leaves the edge unresolved, not the tree
            logger.warning("No provider for %s", dep.dependency_name, exc_info=True)
            return None
        provider = await fetcher.ensure_package(found[0]) if found is not None else None
        if provider is not None and found is not None:
            view.providers[key] = (provider.id, await db.scalar(select(PackageVersion.id).where(
                PackageVersion.package_id == provider.id, PackageVersion.version == found[1])))
    return view.providers[key]


async def _ensure_dependency_resolved(dep: Dependency, fetcher: LazyFetcher) -> object | None:
    """The id of the catalog package an unlinked dependency names, fetched lazily. The row is neither re-linked nor
    re-read here: a sync of the same version running meanwhile replaces its rows, and the one held would be gone (#49).
    The catalog links it when the package enters (`_link_dependents`) and on the next sync."""
    package = await fetcher.ensure_package(_dependency_ref(dep))
    return package.id if package else None


def _measure_tree_depth(nodes: list[DependencyNode]) -> int:
    if not nodes:
        return 0
    return 1 + max(_measure_tree_depth(node.children) for node in nodes)
