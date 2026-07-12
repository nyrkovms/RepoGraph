from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SyncStatus, SyncType
from app.models.registry import Registry
from app.models.sync_state import SyncState


async def get_or_create_sync_state(db: AsyncSession, registry_slug: str = "pypi") -> SyncState:
    registry = await db.scalar(select(Registry).where(Registry.slug == registry_slug))
    if not registry:
        raise RuntimeError(f"Registry '{registry_slug}' is not initialized")

    row = await db.scalar(select(SyncState).where(SyncState.registry_id == registry.id))
    if row:
        return row

    row = SyncState(
        registry_id=registry.id,
        sync_type=SyncType.full,
        status=SyncStatus.idle,
        last_success_at=None,
        last_error=None,
        cursor_data={},
        packages_synced=0,
        packages_total=0,
    )
    db.add(row)
    await db.flush()
    return row


async def get_or_create_pypi_sync_state(db: AsyncSession) -> SyncState:
    return await get_or_create_sync_state(db, "pypi")


async def mark_sync_running(
    db: AsyncSession, sync_type: SyncType, packages_total: int, registry_slug: str = "pypi"
) -> SyncState:
    row = await get_or_create_sync_state(db, registry_slug)
    row.sync_type = sync_type
    row.status = SyncStatus.running
    row.last_error = None
    row.packages_total = packages_total
    row.packages_synced = 0
    return row


async def mark_sync_progress(db: AsyncSession, processed: int, registry_slug: str = "pypi") -> SyncState:
    row = await get_or_create_sync_state(db, registry_slug)
    row.packages_synced = processed
    return row


async def mark_sync_success(
    db: AsyncSession,
    processed: int,
    total: int,
    duration_ms: float | None = None,
    operation: str | None = None,
    registry_slug: str = "pypi",
) -> SyncState:
    row = await get_or_create_sync_state(db, registry_slug)
    row.status = SyncStatus.idle
    row.last_error = None
    row.last_success_at = datetime.now(UTC)
    row.packages_synced = processed
    row.packages_total = total
    cursor_data = dict(row.cursor_data or {})
    if duration_ms is not None:
        cursor_data["last_duration_ms"] = round(float(duration_ms), 2)
    if operation is not None:
        cursor_data["last_operation"] = operation
    row.cursor_data = cursor_data
    return row


async def mark_sync_failed(
    db: AsyncSession,
    error: str,
    processed: int,
    total: int,
    duration_ms: float | None = None,
    operation: str | None = None,
    registry_slug: str = "pypi",
) -> SyncState:
    row = await get_or_create_sync_state(db, registry_slug)
    row.status = SyncStatus.failed
    row.last_error = error[:4000]
    row.packages_synced = processed
    row.packages_total = total
    cursor_data = dict(row.cursor_data or {})
    if duration_ms is not None:
        cursor_data["last_duration_ms"] = round(float(duration_ms), 2)
    if operation is not None:
        cursor_data["last_operation"] = operation
    row.cursor_data = cursor_data
    return row


async def update_sync_cursor(db: AsyncSession, registry_slug: str, updates: dict) -> SyncState:
    row = await get_or_create_sync_state(db, registry_slug)
    cursor_data = dict(row.cursor_data or {})
    cursor_data.update(updates)
    row.cursor_data = cursor_data
    return row
