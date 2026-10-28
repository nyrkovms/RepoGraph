import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import update

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.fetcher.pipeline import pypi_ref, sync_package
from app.models.enums import PackageOrigin, SyncType
from app.models.run import Run
from app.models.sync_failure import SyncFailure
from app.services.metrics import observe_sync_duration
from app.services.runs import FAILED, PARTIAL, SUCCESS, finish_run, start_run
from app.services.sync_failures import clear_sync_failure, list_due_sync_failures, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success
from app.tasks.fetch_packages import ref_from_purl

logger = logging.getLogger(__name__)

# A package that failed on its first fetch enters the catalog with the origin of that operation. The daily update
# only fails for known packages (sync_package keeps the origin of an existing package), hence `dependency`.
_ORIGIN_BY_OPERATION = {
    "seed": PackageOrigin.seed,
    "fetch": PackageOrigin.manual,
    "daily_update": PackageOrigin.dependency,
}


def failure_ref(package_ref: str | None, package_name: str) -> PackageRef:
    """The package of a failure row; rows written before `package_ref` existed hold a bare PyPI name only."""
    return ref_from_purl(package_ref) if package_ref else pypi_ref(package_name)


async def _run_retry_failed_async(limit: int | None = None) -> dict[str, int | str]:
    settings = get_settings()
    started = perf_counter()
    batch_limit = limit if limit is not None else settings.sync_failed_retry_batch_limit

    recovered = 0
    failed = 0
    total = 0

    async with loop_local_session() as db:
        rows = await list_due_sync_failures(db, batch_limit)
        total = len(rows)
        if total == 0:
            # No runs row either: this task fires every few minutes, an empty journal entry each time is noise.
            return {
                "status": "completed",
                "operation": "retry_failed",
                "retried": 0,
                "recovered": 0,
                "failed": 0,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "finished_at": datetime.now(UTC).isoformat(),
            }

        # Plain values: the rollback in the except branches expires the ORM rows.
        failures = [(row.id, row.package_ref, row.package_name, row.operation) for row in rows]

        run = await start_run(db, "retry", None, exclusive=False)
        assert run is not None
        run_id = run.id
        await mark_sync_running(db, SyncType.incremental, packages_total=total)
        await db.commit()

        try:
            for failure_id, package_ref, package_name, operation in failures:
                try:
                    ref = failure_ref(package_ref, package_name)
                except ValueError as exc:
                    # A purl no plugin understands (e.g. its ecosystem was removed): retrying cannot help.
                    failed += 1
                    logger.warning("Retry skipped, bad package_ref %r: %s", package_ref or package_name, exc)
                    await db.execute(
                        update(SyncFailure).where(SyncFailure.id == failure_id).values(retryable=False, next_retry_at=None)
                    )
                    await db.commit()
                    continue

                try:
                    await sync_package(db, ref, origin=_ORIGIN_BY_OPERATION.get(operation, PackageOrigin.dependency), run_id=run_id)
                    await clear_sync_failure(db, ref=ref)
                    recovered += 1
                except PackageNotFound as exc:
                    failed += 1
                    logger.warning("Retry failed package not found: package=%s operation=%s", ref.name, operation)
                    elapsed_ms = (perf_counter() - started) * 1000
                    await db.rollback()
                    await upsert_sync_failure(
                        db,
                        operation=operation,
                        ref=ref,
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
                    logger.exception("Retry failed: package=%s operation=%s", ref.name, operation)
                    elapsed_ms = (perf_counter() - started) * 1000
                    await db.rollback()
                    await upsert_sync_failure(
                        db,
                        operation=operation,
                        ref=ref,
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
        except Exception as exc:
            await db.rollback()
            await _finish(db, run_id, FAILED, total, recovered, failed, error=f"{type(exc).__name__}: {exc}")
            raise

        status = SUCCESS if failed == 0 else PARTIAL if recovered else FAILED
        await _finish(db, run_id, status, total, recovered, failed)

    return {
        "status": "completed",
        "operation": "retry_failed",
        "retried": total,
        "recovered": recovered,
        "failed": failed,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _finish(db, run_id, status: str, total: int, recovered: int, failed: int, error: str | None = None) -> None:
    # The run object was expired by a rollback; load it again before closing.
    run = await db.get(Run, run_id)
    await finish_run(db, run, status, error=error, packages_total=total, packages_checked=total,
                     packages_updated=recovered, packages_failed=failed)
    await db.commit()


@celery_app.task(name="app.tasks.retry_failed.run_retry_failed_sync")
def run_retry_failed_sync(limit: int | None = None) -> dict[str, int | str]:
    return asyncio.run(_run_retry_failed_async(limit=limit))
