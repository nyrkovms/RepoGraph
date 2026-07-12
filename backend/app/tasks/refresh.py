import logging
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.connectors.factory import UnsupportedProtocolError, build_connector, registry_info_from_row
from app.db import SessionLocal
from app.fetcher.pipeline import run_sync_pipeline
from app.fetcher.transport import RegistryAuthError
from app.models.enums import SyncType
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.services.metrics import inc_fetch_error, observe_sync_duration
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success
from app.tasks.runner import run_async

logger = logging.getLogger(__name__)


async def _refresh_registry(db, registry: Registry, limit: int, started: float) -> tuple[int, int]:
    """Refresh oldest-synced packages whose primary registry is `registry`."""
    slug = registry.slug
    try:
        connector = build_connector(registry_info_from_row(registry))
    except UnsupportedProtocolError as exc:
        logger.warning("Refresh skipped: registry=%s error=%s", slug, exc)
        return 0, 0

    rows = (
        await db.scalars(
            select(Package)
            .join(PackageRegistry, PackageRegistry.package_id == Package.id)
            .where(
                PackageRegistry.registry_id == registry.id,
                PackageRegistry.is_primary.is_(True),
            )
            .order_by(Package.synced_at.asc())
            .limit(limit)
        )
    ).all()

    total = len(rows)
    processed = 0
    failed = 0
    if total == 0:
        return 0, 0

    await mark_sync_running(db, SyncType.incremental, packages_total=total, registry_slug=slug)
    await db.commit()

    for package in rows:
        try:
            metadata = await connector.fetch_package_metadata(package.normalized_name)
            await run_sync_pipeline(metadata, db, registry=registry)
            await clear_sync_failure(db, package_name=package.normalized_name)
            processed += 1
        except PackageNotFound as exc:
            failed += 1
            logger.warning("Refresh package not found: registry=%s package=%s", slug, package.normalized_name)
            inc_fetch_error(slug, "not_found")
            await db.rollback()
            await upsert_sync_failure(
                db, operation="refresh", package_name=package.normalized_name, exc=exc, retryable=False,
                ecosystem=registry.ecosystem.value,
            )
            await mark_sync_failed(
                db,
                str(exc),
                processed=processed,
                total=total,
                duration_ms=(perf_counter() - started) * 1000,
                operation="refresh",
                registry_slug=slug,
            )
            await db.commit()
            continue
        except RegistryAuthError as exc:
            failed += 1
            logger.error("Refresh auth error: registry=%s package=%s error=%s", slug, package.normalized_name, exc)
            inc_fetch_error(slug, "auth")
            await db.rollback()
            await mark_sync_failed(
                db,
                str(exc),
                processed=processed,
                total=total,
                duration_ms=(perf_counter() - started) * 1000,
                operation="refresh",
                registry_slug=slug,
            )
            await db.commit()
            # Stale credentials fail every package of this registry the same way.
            break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("Refresh failed: registry=%s package=%s", slug, package.normalized_name)
            inc_fetch_error(slug, exc.__class__.__name__)
            await db.rollback()
            await upsert_sync_failure(db, operation="refresh", package_name=package.normalized_name, exc=exc, ecosystem=registry.ecosystem.value)
            await mark_sync_failed(
                db,
                str(exc),
                processed=processed,
                total=total,
                duration_ms=(perf_counter() - started) * 1000,
                operation="refresh",
                registry_slug=slug,
            )
            await db.commit()
            continue

        await mark_sync_progress(db, processed=processed, registry_slug=slug)
        await db.commit()

    await mark_sync_success(
        db,
        processed=processed,
        total=total,
        duration_ms=(perf_counter() - started) * 1000,
        operation="refresh",
        registry_slug=slug,
    )
    await db.commit()
    return processed, failed


async def _run_refresh_async(limit: int = 500) -> dict[str, int | str]:
    started = perf_counter()
    processed = 0
    failed = 0

    async with SessionLocal() as db:
        registries = (
            await db.scalars(
                select(Registry).where(Registry.sync_enabled.is_(True)).order_by(Registry.sync_priority.asc())
            )
        ).all()

        for registry in registries:
            registry_processed, registry_failed = await _refresh_registry(db, registry, limit, started)
            processed += registry_processed
            failed += registry_failed

        observe_sync_duration("refresh", (perf_counter() - started))

    return {
        "status": "completed",
        "operation": "refresh",
        "limit": limit,
        "registries": len(registries),
        "refreshed": processed,
        "failed": failed,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.refresh.run_batch_refresh")
def run_batch_refresh(limit: int = 500) -> dict[str, int | str]:
    return run_async(_run_refresh_async(limit=limit))
