import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.db import loop_local_session
from app.fetcher.pipeline import plugin_for, sync_package
from app.models.enums import PackageOrigin
from app.services.metrics import observe_sync_duration
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.tasks.fetch_packages import ref_from_target

logger = logging.getLogger(__name__)


async def _fetch_package_async(target: str) -> dict[str, str]:
    """Fetch one package: `target` is a purl (`pkg:pypi/flask`) or a bare PyPI name."""
    started = perf_counter()
    ref = ref_from_target(target)
    coordinates = plugin_for(ref.ecosystem).coordinates
    purl, name = coordinates.to_purl(ref), coordinates.display(ref)

    async with loop_local_session() as db:
        try:
            await sync_package(db, ref, origin=PackageOrigin.manual)
            await clear_sync_failure(db, ref=ref)
            elapsed_ms = (perf_counter() - started) * 1000
            await db.commit()
            observe_sync_duration("fetch", elapsed_ms / 1000.0)
        except PackageNotFound as exc:
            logger.warning("Fetch package not found: package=%s", purl)
            await db.rollback()
            await upsert_sync_failure(
                db,
                operation="fetch",
                ref=ref,
                exc=exc,
                retryable=False,
            )
            await db.commit()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fetch failed: package=%s", purl)
            await db.rollback()
            await upsert_sync_failure(
                db,
                operation="fetch",
                ref=ref,
                exc=exc,
            )
            await db.commit()
            raise

    return {
        "status": "completed",
        "operation": "fetch",
        "name": name,
        "purl": purl,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.fetch_deps.fetch_missing_dependencies")
# `name` stays the parameter name: it is the keyword of tasks queued before purls were accepted.
def fetch_missing_dependencies(name: str) -> dict[str, str]:
    return asyncio.run(_fetch_package_async(name))
