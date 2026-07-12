from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_viewer
from app.classifier import CAPABILITIES, CLASSIFIER_VERSION
from app.classifier.effective import effective_cap_condition
from app.db import get_db_session
from app.models.classification import Classification, ClassificationFacet
from app.models.dependency import Dependency
from app.models.package import Package
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.schemas.system import ClassificationStats, HealthResponse, StatsResponse
from app.services.metrics import render_metrics, update_sync_gauges

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


async def _classification_stats(db: AsyncSession) -> ClassificationStats | None:
    total = await db.scalar(
        select(func.count(Classification.id)).where(Classification.classifier_version == CLASSIFIER_VERSION)
    )
    if not total:
        return None

    status_rows = (
        await db.execute(
            select(Classification.status, func.count(Classification.id))
            .where(Classification.classifier_version == CLASSIFIER_VERSION)
            .group_by(Classification.status)
        )
    ).all()
    role_rows = (
        await db.execute(
            select(ClassificationFacet.role, func.count(ClassificationFacet.id))
            .join(Classification, Classification.id == ClassificationFacet.classification_id)
            .where(Classification.classifier_version == CLASSIFIER_VERSION)
            .group_by(ClassificationFacet.role)
        )
    ).all()

    effective: dict[str, int] = {}
    for capability in CAPABILITIES:
        count = await db.scalar(select(func.count(Package.id)).where(effective_cap_condition(capability)))
        if count:
            effective[capability] = count

    return ClassificationStats(
        classifier_version=CLASSIFIER_VERSION,
        total=total,
        by_status={status: count for status, count in status_rows},
        roles={role: count for role, count in role_rows},
        effective_capabilities=effective,
    )


@router.get("/stats", response_model=StatsResponse, dependencies=[Depends(require_viewer)])
async def stats(db: AsyncSession = Depends(get_db_session)) -> StatsResponse:
    package_count = await db.scalar(select(func.count(Package.id)))
    version_count = await db.scalar(select(func.count(PackageVersion.id)))
    dependency_count = await db.scalar(select(func.count(Dependency.id)))
    last_sync_at = await db.scalar(select(func.max(SyncState.last_success_at)))

    return StatsResponse(
        ecosystem="all",
        packages=package_count or 0,
        versions=version_count or 0,
        dependencies=dependency_count or 0,
        last_sync_at=last_sync_at.isoformat() if last_sync_at else None,
        classification=await _classification_stats(db),
    )


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db_session)) -> Response:
    states = (await db.scalars(select(SyncState))).all()
    update_sync_gauges(states)
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
