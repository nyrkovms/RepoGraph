from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.updates import fetch_last_update_run
from app.auth.tokens import require_admin
from app.db import get_db_session
from app.ecosystems.registry import load_builtin_plugins, registered
from app.models.run import Run
from app.schemas.admin import (
    AdminTaskResponse,
    FetchPackagesRequest,
    PresenceProbeRequest,
    RetryFailedRequest,
    RunInfo,
    SeedRequest,
    SyncStatusResponse,
)
from app.services.sync_state import get_or_create_pypi_sync_state
from app.tasks.fetch_deps import fetch_missing_dependencies
from app.tasks.fetch_packages import fetch_packages, invalid_purls, ref_from_target
from app.tasks.presence_probe import run_presence_probe
from app.tasks.retry_failed import run_retry_failed_sync
from app.tasks.seed import run_seed

router = APIRouter()

RECENT_RUNS = 20


@router.post("/seed", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_seed(payload: SeedRequest = Body(default_factory=SeedRequest)) -> AdminTaskResponse:
    task = run_seed.delay(payload.packages)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="seed")


@router.post("/fetch/{target:path}", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_fetch(target: str) -> AdminTaskResponse:
    """`target` is a purl (`pkg:pypi/flask`, the slashes of a purl belong to the path) or a bare PyPI name."""
    try:
        ref_from_target(target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid package {target!r}: {exc}") from exc
    task = fetch_missing_dependencies.delay(target)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="fetch")


@router.post("/packages", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_fetch_packages(payload: FetchPackagesRequest) -> AdminTaskResponse:
    errors = invalid_purls(payload.purls)
    if errors:
        detail = "; ".join(f"{purl}: {reason}" for purl, reason in errors.items())
        raise HTTPException(status_code=422, detail=f"invalid purls, nothing queued: {detail}")
    task = fetch_packages.delay(payload.purls)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="fetch_packages")


@router.post("/retry-failed", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_retry_failed(payload: RetryFailedRequest = Body(default_factory=RetryFailedRequest)) -> AdminTaskResponse:
    task = run_retry_failed_sync.delay(payload.limit)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="retry_failed")


@router.post("/daily-update", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_daily_update() -> AdminTaskResponse:
    from app.tasks.daily_update import run_daily_update

    task = run_daily_update.delay()
    return AdminTaskResponse(task_id=task.id, status="queued", operation="daily_update")


@router.post("/presence-probe", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_presence_probe(payload: PresenceProbeRequest = Body(default_factory=PresenceProbeRequest)) -> AdminTaskResponse:
    load_builtin_plugins()
    if payload.ecosystem not in {plugin.ecosystem for plugin in registered()}:
        raise HTTPException(status_code=422, detail=f"no plugin for ecosystem {payload.ecosystem!r}")
    task = run_presence_probe.delay(payload.ecosystem)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="presence_probe")


@router.get("/sync/status", response_model=SyncStatusResponse, dependencies=[Depends(require_admin)])
async def sync_status(db: AsyncSession = Depends(get_db_session)) -> SyncStatusResponse:
    row = await get_or_create_pypi_sync_state(db)
    last_update_run = await fetch_last_update_run(db)
    runs = (await db.scalars(select(Run).order_by(Run.started_at.desc()).limit(RECENT_RUNS))).all()
    await db.commit()
    return SyncStatusResponse(
        status=row.status.value,
        sync_type=row.sync_type.value,
        last_success_at=row.last_success_at.isoformat() if row.last_success_at else None,
        last_error=row.last_error,
        packages_synced=row.packages_synced,
        packages_total=row.packages_total,
        cursor_data=row.cursor_data,
        last_update_run=last_update_run,
        recent_runs=[RunInfo.model_validate(run) for run in runs],
    )
