from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.models.dependency import Dependency
from app.models.package import Package
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.schemas.system import HealthResponse, StatsResponse
from app.services.metrics import render_metrics, update_sync_gauges

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/stats", response_model=StatsResponse)
async def stats(db: AsyncSession = Depends(get_db_session)) -> StatsResponse:
    package_count = await db.scalar(select(func.count(Package.id)).where(Package.ecosystem == "pypi"))
    version_count = await db.scalar(select(func.count(PackageVersion.id)))
    dependency_count = await db.scalar(select(func.count(Dependency.id)).where(Dependency.variant_registry_id.is_(None)))
    last_sync_at = await db.scalar(select(func.max(SyncState.last_success_at)))

    return StatsResponse(
        ecosystem="pypi",
        packages=package_count or 0,
        versions=version_count or 0,
        dependencies=dependency_count or 0,
        last_sync_at=last_sync_at.isoformat() if last_sync_at else None,
    )


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db_session)) -> Response:
    states = (await db.scalars(select(SyncState))).all()
    update_sync_gauges(states)
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
