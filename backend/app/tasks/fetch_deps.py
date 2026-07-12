import logging
from datetime import UTC, datetime
from time import perf_counter

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.connectors.factory import build_connector, registry_info_from_row
from app.db import SessionLocal
from app.fetcher.pipeline import get_or_create_default_registry, run_sync_pipeline
from app.models.enums import SyncType
from app.normalizers.names import normalize_name
from app.services.metrics import observe_sync_duration
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success

from app.tasks.runner import run_async

logger = logging.getLogger(__name__)


async def _fetch_package_async(name: str, ecosystem: str = "pypi") -> dict[str, str]:
    started = perf_counter()
    normalized = normalize_name(ecosystem, name)

    async with SessionLocal() as db:
        registry = await get_or_create_default_registry(db, ecosystem)
        connector = build_connector(registry_info_from_row(registry))
        await mark_sync_running(db, SyncType.incremental, packages_total=1, registry_slug=registry.slug)
        await db.commit()

        try:
            metadata = await connector.fetch_package_metadata(normalized)
            await run_sync_pipeline(metadata, db, registry=registry)
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
def fetch_missing_dependencies(name: str, ecosystem: str = "pypi") -> dict[str, str]:
    return run_async(_fetch_package_async(name, ecosystem))
