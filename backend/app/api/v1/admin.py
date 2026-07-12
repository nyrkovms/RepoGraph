from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_admin
from app.connectors.factory import UnsupportedProtocolError, build_connector, registry_info_from_row
from app.db import get_db_session
from app.models.dist_probe import DistProbe
from app.models.enums import Ecosystem, RegistryType
from app.models.package import Package
from app.models.registry import Registry
from app.models.version import PackageVersion
from app.normalizers.names import normalize_package_name
from app.schemas.admin import (
    AdminTaskResponse,
    ProbeDeleteRequest,
    ProbeRequest,
    RefreshRequest,
    RegistryCreateRequest,
    RegistryResponse,
    RegistryTestResponse,
    RegistryUpdateRequest,
    RetryFailedRequest,
    SeedRequest,
    SyncStatusResponse,
)
from app.services.sync_state import get_or_create_pypi_sync_state
from app.tasks.classify import run_classification
from app.tasks.enumeration import run_enumeration
from app.tasks.fetch_deps import fetch_missing_dependencies
from app.tasks.probe import probe_version
from app.tasks.refresh import run_batch_refresh
from app.tasks.retry_failed import run_retry_failed_sync
from app.tasks.seed import run_seed

router = APIRouter()


def _registry_response(row: Registry) -> RegistryResponse:
    return RegistryResponse(
        slug=row.slug,
        ecosystem=row.ecosystem.value,
        name=row.name,
        protocol=row.protocol,
        registry_type=row.registry_type.value,
        base_url=row.base_url,
        api_url=row.api_url,
        has_auth=bool(row.auth_config),
        enumerate_enabled=row.enumerate_enabled,
        sync_enabled=row.sync_enabled,
        sync_priority=row.sync_priority,
    )


@router.post("/seed", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_seed(payload: SeedRequest = Body(default_factory=SeedRequest)) -> AdminTaskResponse:
    task = run_seed.delay(payload.packages)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="seed")


@router.post("/fetch/{name:path}", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_fetch(name: str, ecosystem: str = "pypi") -> AdminTaskResponse:
    if ecosystem not in ("pypi", "npm"):
        raise HTTPException(status_code=422, detail="ecosystem must be pypi or npm")
    task = fetch_missing_dependencies.delay(name, ecosystem)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="fetch")


@router.post("/refresh", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_refresh(payload: RefreshRequest = Body(default_factory=RefreshRequest)) -> AdminTaskResponse:
    task = run_batch_refresh.delay(payload.limit)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="refresh")


@router.post("/retry-failed", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_retry_failed(payload: RetryFailedRequest = Body(default_factory=RetryFailedRequest)) -> AdminTaskResponse:
    task = run_retry_failed_sync.delay(payload.limit)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="retry_failed")


@router.post("/enumerate", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_enumeration() -> AdminTaskResponse:
    task = run_enumeration.delay()
    return AdminTaskResponse(task_id=task.id, status="queued", operation="enumeration")


@router.post("/classify", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_classification(reclassify: bool = False) -> AdminTaskResponse:
    task = run_classification.delay(reclassify)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="classify")


@router.get("/registries", response_model=list[RegistryResponse], dependencies=[Depends(require_admin)])
async def list_registries(db: AsyncSession = Depends(get_db_session)) -> list[RegistryResponse]:
    rows = (await db.scalars(select(Registry).order_by(Registry.sync_priority.asc(), Registry.slug))).all()
    return [_registry_response(row) for row in rows]


@router.post("/registries", response_model=RegistryResponse, status_code=201, dependencies=[Depends(require_admin)])
async def create_registry(
    payload: RegistryCreateRequest, db: AsyncSession = Depends(get_db_session)
) -> RegistryResponse:
    existing = await db.scalar(select(Registry).where(Registry.slug == payload.slug))
    if existing:
        raise HTTPException(status_code=409, detail=f"registry '{payload.slug}' already exists")

    row = Registry(
        ecosystem=Ecosystem(payload.ecosystem),
        name=payload.name,
        slug=payload.slug,
        registry_type=RegistryType(payload.registry_type),
        protocol=payload.protocol,
        base_url=payload.base_url,
        api_url=payload.api_url,
        auth_config=payload.auth_config,
        auth_required=bool(payload.auth_config),
        enumerate_enabled=payload.enumerate_enabled,
        sync_enabled=payload.sync_enabled,
        sync_priority=payload.sync_priority,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _registry_response(row)


@router.put("/registries/{slug}", response_model=RegistryResponse, dependencies=[Depends(require_admin)])
async def update_registry(
    slug: str, payload: RegistryUpdateRequest, db: AsyncSession = Depends(get_db_session)
) -> RegistryResponse:
    row = await db.scalar(select(Registry).where(Registry.slug == slug))
    if not row:
        raise HTTPException(status_code=404, detail=f"registry '{slug}' not found")

    updates = payload.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(row, field_name, value)
    if "auth_config" in updates:
        row.auth_required = bool(row.auth_config)
    await db.commit()
    await db.refresh(row)
    return _registry_response(row)


@router.post("/registries/{slug}/test", response_model=RegistryTestResponse, dependencies=[Depends(require_admin)])
async def test_registry(slug: str, db: AsyncSession = Depends(get_db_session)) -> RegistryTestResponse:
    row = await db.scalar(select(Registry).where(Registry.slug == slug))
    if not row:
        raise HTTPException(status_code=404, detail=f"registry '{slug}' not found")
    try:
        connector = build_connector(registry_info_from_row(row))
    except UnsupportedProtocolError as exc:
        return RegistryTestResponse(ok=False, detail=str(exc))
    ok, detail = await connector.test_connection()
    return RegistryTestResponse(ok=ok, detail=detail)


async def _find_version_row(db: AsyncSession, name: str, version: str | None) -> PackageVersion:
    normalized = normalize_package_name(name)
    package = await db.scalar(
        select(Package).where(Package.ecosystem == Ecosystem.pypi, Package.normalized_name == normalized)
    )
    if not package:
        raise HTTPException(status_code=404, detail=f"package '{normalized}' not found")
    target_version = version or package.latest_version
    row = await db.scalar(
        select(PackageVersion).where(
            PackageVersion.package_id == package.id, PackageVersion.version == target_version
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"version '{target_version}' not found for '{normalized}'")
    return row


@router.post("/probe", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def trigger_probe(payload: ProbeRequest, db: AsyncSession = Depends(get_db_session)) -> AdminTaskResponse:
    row = await _find_version_row(db, payload.name, payload.version)
    task = probe_version.delay(str(row.id), payload.artifact_filename)
    return AdminTaskResponse(task_id=task.id, status="queued", operation="probe")


@router.post("/probe/delete", response_model=AdminTaskResponse, dependencies=[Depends(require_admin)])
async def delete_probe(payload: ProbeDeleteRequest, db: AsyncSession = Depends(get_db_session)) -> AdminTaskResponse:
    row = await _find_version_row(db, payload.name, payload.version)
    stmt = delete(DistProbe).where(DistProbe.version_id == row.id)
    if payload.artifact_filename:
        stmt = stmt.where(DistProbe.artifact_filename == payload.artifact_filename)
    await db.execute(stmt)
    await db.commit()
    return AdminTaskResponse(task_id="", status="deleted", operation="probe_delete")


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
