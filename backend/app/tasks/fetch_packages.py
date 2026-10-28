import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.ecosystems.purl import parse as parse_purl
from app.ecosystems.registry import registered
from app.fetcher.pipeline import load_builtin_plugins, sync_package
from app.models.enums import PackageOrigin
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure

logger = logging.getLogger(__name__)


def ref_from_purl(purl: str) -> PackageRef:
    """The package a purl names, through the plugin of its type. Raises ValueError with a readable reason."""
    parsed = parse_purl(purl)
    load_builtin_plugins()
    for plugin in registered():
        if plugin.purl_type == parsed.type:
            return plugin.coordinates.from_purl(purl)[0]
    raise ValueError(f"no plugin for purl type {parsed.type!r}")


def invalid_purls(purls: list[str]) -> dict[str, str]:
    errors = {}
    for purl in purls:
        try:
            ref_from_purl(purl)
        except ValueError as exc:
            errors[purl] = str(exc)
    return errors


async def _fetch_packages_async(purls: list[str]) -> dict[str, object]:
    started = perf_counter()
    counts = {"fetched": 0, "not_found": 0, "failed": 0}

    async with loop_local_session() as db:
        for purl in purls:
            try:
                ref = ref_from_purl(purl)
            except ValueError:
                counts["failed"] += 1
                logger.warning("Fetch skipped, bad purl: %s", purl)
                continue
            # sync_failures is keyed by bare PyPI names until B4 moves it to purls (IMPL T9), so only PyPI
            # failures can be recorded there without being retried as the wrong ecosystem.
            records_failures = ref.ecosystem == "pypi"
            try:
                await sync_package(db, ref, origin=PackageOrigin.manual)
                if records_failures:
                    await clear_sync_failure(db, package_name=ref.name)
                await db.commit()
                counts["fetched"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad package must not stop the batch
                not_found = isinstance(exc, PackageNotFound)
                counts["not_found" if not_found else "failed"] += 1
                if not_found:
                    logger.warning("Fetch package not found: %s", purl)
                else:
                    logger.exception("Fetch failed: %s", purl)
                await db.rollback()
                if records_failures:
                    await upsert_sync_failure(db, operation="fetch", package_name=ref.name, exc=exc,
                                              retryable=False if not_found else None)
                    await db.commit()

    return {
        "status": "completed",
        "operation": "fetch_packages",
        "requested": len(purls),
        **counts,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.fetch_packages.fetch_packages")
def fetch_packages(purls: list[str]) -> dict[str, object]:
    return asyncio.run(_fetch_packages_async(purls))
