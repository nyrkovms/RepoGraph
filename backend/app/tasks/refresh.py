import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.db import SessionLocal
from app.fetcher.pipeline import pypi_ref, sync_package
from app.models.enums import SyncType
from app.models.package import Package
from app.services.metrics import observe_sync_duration
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success

logger = logging.getLogger(__name__)


async def _run_refresh_async(limit: int = 500) -> dict[str, int | str]:
    started = perf_counter()

    async with SessionLocal() as db:
        rows = (
            await db.scalars(
                select(Package)
                .where(Package.ecosystem == "pypi")
                .order_by(Package.synced_at.asc())
                .limit(limit)
            )
        ).all()

        total = len(rows)
        processed = 0
        failed = 0

        await mark_sync_running(db, SyncType.incremental, packages_total=total)
        await db.commit()

        for package in rows:
            try:
                await sync_package(db, pypi_ref(package.normalized_name))
                await clear_sync_failure(db, package_name=package.normalized_name)
                processed += 1
            except PackageNotFound as exc:
                failed += 1
                logger.warning("Refresh package not found: package=%s", package.normalized_name)
                elapsed_ms = (perf_counter() - started) * 1000
                await db.rollback()
                await upsert_sync_failure(
                    db,
                    operation="refresh",
                    package_name=package.normalized_name,
                    exc=exc,
                    retryable=False,
                )
                await mark_sync_failed(
                    db,
                    str(exc),
                    processed=processed,
                    total=total,
                    duration_ms=elapsed_ms,
                    operation="refresh",
                )
                await db.commit()
                continue
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception("Refresh failed: package=%s", package.normalized_name)
                elapsed_ms = (perf_counter() - started) * 1000
                await db.rollback()
                await upsert_sync_failure(
                    db,
                    operation="refresh",
                    package_name=package.normalized_name,
                    exc=exc,
                )
                await mark_sync_failed(
                    db,
                    str(exc),
                    processed=processed,
                    total=total,
                    duration_ms=elapsed_ms,
                    operation="refresh",
                )
                await db.commit()
                continue

            await mark_sync_progress(db, processed=processed)
            await db.commit()

        elapsed_ms = (perf_counter() - started) * 1000
        await mark_sync_success(
            db,
            processed=processed,
            total=total,
            duration_ms=elapsed_ms,
            operation="refresh",
        )
        await db.commit()
        observe_sync_duration("refresh", elapsed_ms / 1000.0)

    return {
        "status": "completed",
        "operation": "refresh",
        "limit": limit,
        "refreshed": processed,
        "failed": failed,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.refresh.run_batch_refresh")
def run_batch_refresh(limit: int = 500) -> dict[str, int | str]:
    return asyncio.run(_run_refresh_async(limit=limit))
