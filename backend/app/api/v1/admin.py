from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.updates import fetch_last_update_run
from app.auth.tokens import require_admin
from app.db import get_db_session
from app.ecosystems.registry import load_builtin_plugins, registered
from app.models.enums import Ecosystem
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.schemas.admin import (
    AdminTaskResponse,
    FetchPackagesRequest,
    PresenceProbeRequest,
    RegistryState,
    RetryFailedRequest,
    RunInfo,
    SeedRequest,
    SyncStatusResponse,
)
from app.services.runs import FAILED, RUNNING
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


def sync_summary(last_run: Run | None) -> dict[str, object]:
    """The old single-source keys of /sync/status as a summary of the latest run of any operation."""
    if last_run is None:
        return {"status": "idle", "sync_type": "incremental", "packages_synced": 0, "packages_total": 0, "last_error": None}
    return {
        "status": last_run.status if last_run.status in (RUNNING, FAILED) else "idle",
        "sync_type": "full" if last_run.operation == "seed" else "incremental",
        "packages_synced": last_run.packages_checked,
        "packages_total": last_run.packages_total,
        "last_error": last_run.error,
    }


@router.get("/sync/status", response_model=SyncStatusResponse, dependencies=[Depends(require_admin)])
async def sync_status(db: AsyncSession = Depends(get_db_session)) -> SyncStatusResponse:
    last_update_run = await fetch_last_update_run(db)
    runs = (await db.scalars(select(Run).order_by(Run.started_at.desc()).limit(RECENT_RUNS))).all()
    rows = (
        await db.execute(
            select(Registry, SyncState).outerjoin(SyncState, SyncState.registry_id == Registry.id)
        )
    ).all()
    states = sorted(rows, key=lambda row: (row[0].ecosystem.value, row[0].priority, row[0].slug))
    pypi_states = [state for registry, state in states if registry.ecosystem == Ecosystem.pypi and state is not None]
    last_success = max((s.last_success_at for s in pypi_states if s.last_success_at), default=None)
    pypi_primary = next((state for registry, state in states if registry.slug == "pypi" and state is not None), None)
    return SyncStatusResponse(
        **sync_summary(runs[0] if runs else None),
        last_success_at=last_success.isoformat() if last_success else None,
        cursor_data=pypi_primary.cursor_data if pypi_primary else {},
        last_update_run=last_update_run,
        recent_runs=[RunInfo.model_validate(run) for run in runs],
        registries=[
            RegistryState(
                slug=registry.slug,
                ecosystem=registry.ecosystem,
                enabled=registry.enabled,
                last_success_at=state.last_success_at if state else None,
                last_error=state.last_error if state else None,
                consecutive_failures=state.consecutive_failures if state else 0,
                degraded_until=state.degraded_until if state else None,
                cursor_data=state.cursor_data if state else {},
            )
            for registry, state in states
        ],
    )
