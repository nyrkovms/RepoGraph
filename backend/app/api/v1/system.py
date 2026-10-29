from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.models.dependency import Dependency
from app.models.enums import Ecosystem
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.sync_state import SyncState
from app.models.version import PackageVersion, VersionSource
from app.schemas.system import EcosystemStats, HealthResponse, RegistryStats, StatsResponse
from app.services.metrics import render_metrics, update_source_gauges, update_sync_gauges

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/stats", response_model=StatsResponse)
async def stats(db: AsyncSession = Depends(get_db_session)) -> StatsResponse:
    packages = dict((await db.execute(select(Package.ecosystem, func.count(Package.id)).group_by(Package.ecosystem))).all())
    versions = dict(
        (
            await db.execute(
                select(Package.ecosystem, func.count(PackageVersion.id))
                .join(Package, Package.id == PackageVersion.package_id)
                .group_by(Package.ecosystem)
            )
        ).all()
    )
    dependencies = dict(
        (
            await db.execute(
                select(Package.ecosystem, func.count(Dependency.id))
                .join(PackageVersion, PackageVersion.id == Dependency.version_id)
                .join(Package, Package.id == PackageVersion.package_id)
                .where(Dependency.variant_registry_id.is_(None))
                .group_by(Package.ecosystem)
            )
        ).all()
    )
    registry_rows = (
        await db.execute(
            select(Registry, SyncState.last_success_at, SyncState.degraded_until)
            .outerjoin(SyncState, SyncState.registry_id == Registry.id)
            .order_by(Registry.priority, Registry.slug)
        )
    ).all()
    registry_packages = dict(
        (
            await db.execute(
                select(PackageRegistry.registry_id, func.count())
                .where(PackageRegistry.missing_since.is_(None))
                .group_by(PackageRegistry.registry_id)
            )
        ).all()
    )
    registry_versions = dict(
        (await db.execute(select(VersionSource.registry_id, func.count()).group_by(VersionSource.registry_id))).all()
    )

    now = datetime.now(UTC)
    registries: dict[Ecosystem, list[RegistryStats]] = {}
    last_sync: dict[Ecosystem, datetime] = {}
    for registry, last_success_at, degraded_until in registry_rows:
        registries.setdefault(registry.ecosystem, []).append(
            RegistryStats(
                slug=registry.slug,
                enabled=registry.enabled,
                packages=registry_packages.get(registry.id, 0),
                versions=registry_versions.get(registry.id, 0),
                last_success_at=last_success_at.isoformat() if last_success_at else None,
                degraded=degraded_until is not None and degraded_until > now,
            )
        )
        if last_success_at and (registry.ecosystem not in last_sync or last_success_at > last_sync[registry.ecosystem]):
            last_sync[registry.ecosystem] = last_success_at

    items = [
        EcosystemStats(
            ecosystem=ecosystem.value,
            packages=packages.get(ecosystem, 0),
            versions=versions.get(ecosystem, 0),
            dependencies=dependencies.get(ecosystem, 0),
            last_sync_at=last_sync[ecosystem].isoformat() if ecosystem in last_sync else None,
            registries=registries.get(ecosystem, []),
        )
        for ecosystem in sorted(registries.keys() | packages.keys(), key=lambda e: e.value)
    ]
    pypi = next((item for item in items if item.ecosystem == Ecosystem.pypi.value), None)
    return StatsResponse(
        ecosystem=Ecosystem.pypi.value,
        packages=pypi.packages if pypi else 0,
        versions=pypi.versions if pypi else 0,
        dependencies=pypi.dependencies if pypi else 0,
        last_sync_at=pypi.last_sync_at if pypi else None,
        ecosystems=items,
    )


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db_session)) -> Response:
    states = (await db.scalars(select(SyncState))).all()
    update_sync_gauges(states)
    per_source = (await db.execute(select(Registry.ecosystem, Registry.slug, SyncState)
                                   .join(SyncState, SyncState.registry_id == Registry.id))).all()
    update_source_gauges(((eco.value, slug, state) for eco, slug, state in per_source), datetime.now(UTC))
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
