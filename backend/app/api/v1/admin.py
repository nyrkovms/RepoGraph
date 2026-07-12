from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_admin
from app.db import get_db_session
from app.schemas.admin import AdminTaskResponse, RefreshRequest, RetryFailedRequest, SeedRequest, SyncStatusResponse
from app.services.sync_state import get_or_create_pypi_sync_state
from app.tasks.fetch_deps import fetch_missing_dependencies
from app.tasks.refresh import run_batch_refresh
from app.tasks.retry_failed import run_retry_failed_sync
from app.tasks.seed import run_seed

router = APIRouter()


@router.post("/seed", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_seed(payload: SeedRequest = Body(default_factory=SeedRequest)) -> AdminTaskResponse:
    task = run_seed.delay(payload.packages)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="seed")


@router.post("/fetch/{name}", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_fetch(name: str) -> AdminTaskResponse:
    task = fetch_missing_dependencies.delay(name)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="fetch")


@router.post("/refresh", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_refresh(payload: RefreshRequest = Body(default_factory=RefreshRequest)) -> AdminTaskResponse:
    task = run_batch_refresh.delay(payload.limit)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="refresh")


@router.post("/retry-failed", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_retry_failed(payload: RetryFailedRequest = Body(default_factory=RetryFailedRequest)) -> AdminTaskResponse:
    task = run_retry_failed_sync.delay(payload.limit)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="retry_failed")


@router.get("/sync/status", response_model=SyncStatusResponse, dependencies=[Depends(require_admin)])
async def sync_status(db: AsyncSession = Depends(get_db_session)) -> SyncStatusResponse:
    row = await get_or_create_pypi_sync_state(db)
    await db.commit()
    return SyncStatusResponse(
        status=row.status.value,
        sync_type=row.sync_type.value,
        last_success_at=row.last_success_at.isoformat() if row.last_success_at else None,
        last_error=row.last_error,
        packages_synced=row.packages_synced,
        packages_total=row.packages_total,
        cursor_data=row.cursor_data,
    )
