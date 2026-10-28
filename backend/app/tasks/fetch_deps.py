import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.db import SessionLocal
from app.fetcher.pipeline import pypi_ref, sync_package
from app.models.enums import PackageOrigin
from app.models.enums import SyncType
from app.normalizers.names import normalize_package_name
from app.services.metrics import observe_sync_duration
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success

logger = logging.getLogger(__name__)


async def _fetch_package_async(name: str) -> dict[str, str]:
    started = perf_counter()
    normalized = normalize_package_name(name)

    async with SessionLocal() as db:
        await mark_sync_running(db, SyncType.incremental, packages_total=1)
        await db.commit()

        try:
            await sync_package(db, pypi_ref(normalized), origin=PackageOrigin.manual)
            await clear_sync_failure(db, package_name=normalized)
            await mark_sync_progress(db, processed=1)
            elapsed_ms = (perf_counter() - started) * 1000
            await mark_sync_success(
                db,
                processed=1,
                total=1,
                duration_ms=elapsed_ms,
                operation="fetch",
            )
            await db.commit()
            observe_sync_duration("fetch", elapsed_ms / 1000.0)
        except PackageNotFound as exc:
            logger.warning("Fetch package not found: package=%s", normalized)
            elapsed_ms = (perf_counter() - started) * 1000
            await db.rollback()
            await upsert_sync_failure(
                db,
                operation="fetch",
                package_name=normalized,
                exc=exc,
                retryable=False,
            )
            await mark_sync_failed(
                db,
                str(exc),
                processed=0,
                total=1,
                duration_ms=elapsed_ms,
                operation="fetch",
            )
            await db.commit()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fetch failed: package=%s", normalized)
            elapsed_ms = (perf_counter() - started) * 1000
            await db.rollback()
            await upsert_sync_failure(
                db,
                operation="fetch",
                package_name=normalized,
                exc=exc,
            )
            await mark_sync_failed(
                db,
                str(exc),
                processed=0,
                total=1,
                duration_ms=elapsed_ms,
                operation="fetch",
            )
            await db.commit()
            raise

    return {
        "status": "completed",
        "operation": "fetch",
        "name": normalized,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.fetch_deps.fetch_missing_dependencies")
def fetch_missing_dependencies(name: str) -> dict[str, str]:
    return asyncio.run(_fetch_package_async(name))
