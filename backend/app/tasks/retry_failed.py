import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.ecosystems.pypi.connector import PyPIConnector
from app.db import SessionLocal
from app.fetcher.pipeline import run_sync_pipeline
from app.models.enums import SyncType
from app.services.metrics import observe_sync_duration
from app.services.sync_failures import clear_sync_failure, list_due_sync_failures, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success

logger = logging.getLogger(__name__)


async def _run_retry_failed_async(limit: int | None = None) -> dict[str, int | str]:
    settings = get_settings()
    started = perf_counter()
    connector = PyPIConnector()
    batch_limit = limit if limit is not None else settings.sync_failed_retry_batch_limit

    recovered = 0
    failed = 0
    total = 0

    async with SessionLocal() as db:
        rows = await list_due_sync_failures(db, batch_limit)
        total = len(rows)
        if total == 0:
            return {
                "status": "completed",
                "operation": "retry_failed",
                "retried": 0,
                "recovered": 0,
                "failed": 0,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "finished_at": datetime.now(UTC).isoformat(),
            }

        await mark_sync_running(db, SyncType.incremental, packages_total=total)
        await db.commit()

        for row in rows:
            try:
                metadata = await connector.fetch_package_metadata(row.package_name)
                await run_sync_pipeline(metadata, db)
                await clear_sync_failure(db, package_name=row.package_name)
                recovered += 1
            except PackageNotFound as exc:
                failed += 1
                logger.warning("Retry failed package not found: package=%s operation=%s", row.package_name, row.operation)
                elapsed_ms = (perf_counter() - started) * 1000
                await db.rollback()
                await upsert_sync_failure(
                    db,
                    operation=row.operation,
                    package_name=row.package_name,
                    exc=exc,
                    retryable=False,
                )
                await mark_sync_failed(
                    db,
                    str(exc),
                    processed=recovered,
                    total=total,
                    duration_ms=elapsed_ms,
                    operation="retry_failed",
                )
                await db.commit()
                continue
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception("Retry failed: package=%s operation=%s", row.package_name, row.operation)
                elapsed_ms = (perf_counter() - started) * 1000
                await db.rollback()
                await upsert_sync_failure(
                    db,
                    operation=row.operation,
                    package_name=row.package_name,
                    exc=exc,
                )
                await mark_sync_failed(
                    db,
                    str(exc),
                    processed=recovered,
                    total=total,
                    duration_ms=elapsed_ms,
                    operation="retry_failed",
                )
                await db.commit()
                continue

            await mark_sync_progress(db, processed=recovered)
            await db.commit()

        elapsed_ms = (perf_counter() - started) * 1000
        await mark_sync_success(
            db,
            processed=recovered,
            total=total,
            duration_ms=elapsed_ms,
            operation="retry_failed",
        )
        await db.commit()
        observe_sync_duration("retry_failed", elapsed_ms / 1000.0)

    return {
        "status": "completed",
        "operation": "retry_failed",
        "retried": total,
        "recovered": recovered,
        "failed": failed,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.retry_failed.run_retry_failed_sync")
def run_retry_failed_sync(limit: int | None = None) -> dict[str, int | str]:
    return asyncio.run(_run_retry_failed_async(limit=limit))
