"""Enumeration loop (D13): full indexing of registries that support listing.

For every registry with enumerate_enabled the task pulls the complete list of
package names (simple index root / internal catalog), takes the next batch
after the stored cursor, syncs the names missing from the catalog, and saves
the cursor in SyncState. When the pass completes, the cursor resets so the
next cycle re-checks for newly published packages.
"""

import logging
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.connectors.factory import UnsupportedProtocolError, build_connector, registry_info_from_row
from app.db import SessionLocal
from app.fetcher.pipeline import find_package, run_sync_pipeline
from app.fetcher.transport import RegistryAuthError
from app.models.registry import Registry
from app.normalizers.names import normalize_package_name
from app.services.metrics import inc_enumeration_synced, inc_fetch_error, observe_sync_duration
from app.services.sync_failures import upsert_sync_failure
from app.services.sync_state import get_or_create_sync_state, update_sync_cursor
from app.tasks.runner import run_async

logger = logging.getLogger(__name__)

CURSOR_KEY = "enumeration_cursor"


def select_enumeration_batch(names: list[str], cursor: str, batch_limit: int) -> tuple[list[str], str, bool]:
    """Pure batching over the sorted full listing.

    Returns (batch, new_cursor, completed): names strictly after `cursor`,
    at most batch_limit; completed=True when the listing is exhausted and the
    cursor resets for the next full pass.
    """
    ordered = sorted(set(names))
    remaining = [n for n in ordered if n > cursor]
    batch = remaining[:batch_limit]
    if len(remaining) <= batch_limit:
        return batch, "", True
    return batch, batch[-1], False


async def _enumerate_registry(db, registry: Registry, batch_limit: int) -> dict[str, int | str | bool]:
    slug = registry.slug
    try:
        connector = build_connector(registry_info_from_row(registry))
    except UnsupportedProtocolError as exc:
        logger.warning("Enumeration skipped: registry=%s error=%s", slug, exc)
        return {"registry": slug, "skipped": True, "reason": str(exc)}

    try:
        names = await connector.list_package_names()
    except NotImplementedError:
        logger.warning("Enumeration skipped: registry=%s protocol has no listing", slug)
        return {"registry": slug, "skipped": True, "reason": "protocol has no listing"}
    except RegistryAuthError as exc:
        logger.error("Enumeration auth error: registry=%s error=%s", slug, exc)
        inc_fetch_error(slug, "auth")
        return {"registry": slug, "skipped": True, "reason": "auth error"}

    state = await get_or_create_sync_state(db, slug)
    cursor = str((state.cursor_data or {}).get(CURSOR_KEY, ""))
    batch, new_cursor, completed = select_enumeration_batch(names, cursor, batch_limit)

    synced = 0
    failed = 0
    for name in batch:
        normalized = normalize_package_name(name)
        if await find_package(db, registry.ecosystem.value, normalized):
            continue
        try:
            metadata = await connector.fetch_package_metadata(normalized)
            await run_sync_pipeline(metadata, db, registry=registry)
            synced += 1
        except PackageNotFound:
            # Listed but page missing — a registry inconsistency worth logging only.
            logger.warning("Enumeration: listed but not fetchable: registry=%s package=%s", slug, normalized)
            inc_fetch_error(slug, "not_found")
            await db.rollback()
        except RegistryAuthError as exc:
            logger.error("Enumeration auth error mid-batch: registry=%s error=%s", slug, exc)
            inc_fetch_error(slug, "auth")
            await db.rollback()
            break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("Enumeration sync failed: registry=%s package=%s", slug, normalized)
            inc_fetch_error(slug, exc.__class__.__name__)
            await db.rollback()
            await upsert_sync_failure(db, operation="enumeration", package_name=normalized, exc=exc)
            await db.commit()

    updates: dict = {CURSOR_KEY: new_cursor, "enumeration_total_names": len(set(names))}
    if completed:
        updates["enumeration_completed_at"] = datetime.now(UTC).isoformat()
    await update_sync_cursor(db, slug, updates)
    await db.commit()

    if synced:
        inc_enumeration_synced(slug, synced)
    return {
        "registry": slug,
        "listed": len(set(names)),
        "batch": len(batch),
        "synced": synced,
        "failed": failed,
        "completed_pass": completed,
    }


async def _run_enumeration_async(batch_limit: int | None = None) -> dict:
    settings = get_settings()
    started = perf_counter()
    limit = batch_limit if batch_limit is not None else settings.enumeration_batch_limit

    results = []
    async with SessionLocal() as db:
        registries = (
            await db.scalars(
                select(Registry)
                .where(Registry.sync_enabled.is_(True), Registry.enumerate_enabled.is_(True))
                .order_by(Registry.sync_priority.asc())
            )
        ).all()
        for registry in registries:
            results.append(await _enumerate_registry(db, registry, limit))

    observe_sync_duration("enumeration", perf_counter() - started)
    return {
        "status": "completed",
        "operation": "enumeration",
        "registries": results,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.enumeration.run_enumeration")
def run_enumeration(batch_limit: int | None = None) -> dict:
    return run_async(_run_enumeration_async(batch_limit=batch_limit))
