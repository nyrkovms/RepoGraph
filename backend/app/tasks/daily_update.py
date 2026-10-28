import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.ecosystems.pypi.simple_index import PyPISimpleIndex
from app.db import loop_local_session
from app.fetcher.pipeline import mark_unavailable, pypi_ref, sync_package
from app.models.enums import Ecosystem, UpdateRunStatus
from app.models.package import Package
from app.models.package_event import PackageEvent
from app.models.update_run import UpdateRun
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure

logger = logging.getLogger(__name__)

OPERATION = "daily_update"
RUN_OVERLAP_GUARD = timedelta(hours=6)


@dataclass(slots=True)
class TrackedPackage:
    id: object
    name: str
    last_serial: int | None
    unavailable: bool


def plan_updates(
    tracked: list[TrackedPackage], index_serials: dict[str, int]
) -> tuple[list[tuple[TrackedPackage, int]], list[TrackedPackage]]:
    """Split catalog packages into changed (with the serial PyPI reports) and missing from the index."""
    changed: list[tuple[TrackedPackage, int]] = []
    missing: list[TrackedPackage] = []
    for package in tracked:
        serial = index_serials.get(package.name)
        if serial is None:
            # Already known to be unavailable: nothing new to report until it comes back.
            if not package.unavailable:
                missing.append(package)
            continue
        if package.unavailable or serial > (package.last_serial or 0):
            changed.append((package, serial))
    return changed, missing


async def _load_tracked_packages(db) -> list[TrackedPackage]:
    rows = (
        await db.execute(
            select(Package.id, Package.normalized_name, Package.last_serial, Package.unavailable_at).where(
                Package.ecosystem == Ecosystem.pypi
            )
        )
    ).all()
    return [
        TrackedPackage(id=row.id, name=row.normalized_name, last_serial=row.last_serial, unavailable=row.unavailable_at is not None)
        for row in rows
    ]


async def _fetch_serials_per_project(index: PyPISimpleIndex, tracked: list[TrackedPackage], concurrency: int) -> dict[str, int]:
    semaphore = asyncio.Semaphore(concurrency)
    serials: dict[str, int] = {}

    async def one(package: TrackedPackage) -> None:
        async with semaphore:
            serial = await index.fetch_project_serial(package.name)
        if serial is not None:
            serials[package.name] = serial

    await asyncio.gather(*(one(package) for package in tracked))
    return serials


async def _mark_unavailable(db, package_id, run_id) -> bool:
    package = await db.scalar(select(Package).where(Package.id == package_id))
    if package is None:
        return False
    return await mark_unavailable(db, package, run_id)


async def _update_package(package: TrackedPackage, expected_serial: int | None, run_id) -> str:
    """Fetch one package from its sources and write it. Returns updated | stale | unavailable | failed."""
    async with loop_local_session() as db:
        try:
            written = await sync_package(db, pypi_ref(package.name), run_id=run_id)
            await clear_sync_failure(db, package_name=package.name, operation=OPERATION)
            await db.commit()
        except PackageNotFound:
            await db.rollback()
            newly = await _mark_unavailable(db, package.id, run_id)
            await clear_sync_failure(db, package_name=package.name, operation=OPERATION)
            await db.commit()
            return "unavailable" if newly else "updated"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Daily update failed: package=%s error=%s", package.name, exc.__class__.__name__)
            await db.rollback()
            await upsert_sync_failure(db, operation=OPERATION, package_name=package.name, exc=exc)
            await db.commit()
            return "failed"

    # PyPI JSON is served through a CDN and may lag behind the index right after a release.
    if expected_serial is not None and written.last_serial is not None and written.last_serial < expected_serial:
        return "stale"
    return "updated"


async def _update_many(
    items: list[tuple[TrackedPackage, int | None]], run_id, concurrency: int
) -> dict[str, list[tuple[TrackedPackage, int | None]]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, list[tuple[TrackedPackage, int | None]]] = {"updated": [], "stale": [], "unavailable": [], "failed": []}

    async def one(item: tuple[TrackedPackage, int | None]) -> None:
        async with semaphore:
            outcome = await _update_package(item[0], item[1], run_id)
        results[outcome].append(item)

    await asyncio.gather(*(one(item) for item in items))
    return results


async def _run_daily_update_async() -> dict:
    settings = get_settings()
    started = perf_counter()
    concurrency = max(1, settings.daily_update_concurrency)

    async with loop_local_session() as db:
        running = await db.scalar(
            select(UpdateRun).where(
                UpdateRun.status == UpdateRunStatus.running,
                UpdateRun.started_at > datetime.now(UTC) - RUN_OVERLAP_GUARD,
            )
        )
        if running is not None:
            logger.warning("Daily update skipped: run %s is still running", running.id)
            return {"status": "skipped", "operation": OPERATION, "running_run_id": str(running.id)}

        run = UpdateRun(status=UpdateRunStatus.running)
        db.add(run)
        await db.commit()
        run_id = run.id
        tracked = await _load_tracked_packages(db)

    index = PyPISimpleIndex()
    index_serial: int | None = None
    try:
        try:
            index_serial, index_serials = await index.fetch_all_serials()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Simple index unavailable (%s), falling back to per-project serials", exc.__class__.__name__)
            index_serials = await _fetch_serials_per_project(index, tracked, concurrency)

        changed, missing = plan_updates(tracked, index_serials)
        logger.info("Daily update: checked=%s changed=%s missing=%s", len(tracked), len(changed), len(missing))

        results = await _update_many(changed + [(package, None) for package in missing], run_id, concurrency)

        if results["stale"]:
            await asyncio.sleep(settings.daily_update_stale_retry_seconds)
            retry = await _update_many(results["stale"], run_id, concurrency)
            if retry["stale"]:
                logger.warning("Daily update: %s packages still stale, next run will pick them up", len(retry["stale"]))
            results["stale"] = retry["stale"]
            for outcome in ("updated", "unavailable", "failed"):
                results[outcome].extend(retry[outcome])

        status = UpdateRunStatus.success if not results["failed"] else UpdateRunStatus.partial
        error = None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Daily update failed")
        results = {"updated": [], "stale": [], "unavailable": [], "failed": []}
        changed = []
        status = UpdateRunStatus.failed
        error = f"{exc.__class__.__name__}: {exc}"[:2000]

    async with loop_local_session() as db:
        run = await db.scalar(select(UpdateRun).where(UpdateRun.id == run_id))
        events_by_kind = dict(
            (
                await db.execute(
                    select(PackageEvent.kind, func.count(PackageEvent.id))
                    .where(PackageEvent.run_id == run_id)
                    .group_by(PackageEvent.kind)
                )
            ).all()
        )
        run.finished_at = datetime.now(UTC)
        run.status = status
        run.error = error
        run.index_serial = index_serial
        run.packages_checked = len(tracked)
        run.packages_changed = len(changed)
        run.packages_updated = len(results["updated"]) + len(results["stale"])
        run.packages_failed = len(results["failed"])
        run.packages_unavailable = len(results["unavailable"])
        run.events_created = sum(events_by_kind.values())
        await db.commit()

        summary = {
            "status": status.value,
            "operation": OPERATION,
            "run_id": str(run_id),
            "index_serial": index_serial,
            "checked": run.packages_checked,
            "changed": run.packages_changed,
            "updated": run.packages_updated,
            "failed": run.packages_failed,
            "unavailable": run.packages_unavailable,
            "events": {kind.value: count for kind, count in events_by_kind.items()},
            "duration_ms": round((perf_counter() - started) * 1000, 2),
            "finished_at": run.finished_at.isoformat(),
        }

    _record_metrics(summary, events_by_kind)
    return summary


def _record_metrics(summary: dict, events_by_kind: dict) -> None:
    from app.services.metrics import inc_package_events, record_daily_update_run

    record_daily_update_run(
        status=summary["status"],
        duration_seconds=summary["duration_ms"] / 1000.0,
        updated=summary["updated"],
        failed=summary["failed"],
        unavailable=summary["unavailable"],
        finished_at_ts=datetime.fromisoformat(summary["finished_at"]).timestamp(),
    )
    for kind, count in events_by_kind.items():
        inc_package_events(kind.value, count)


@celery_app.task(name="app.tasks.daily_update.run_daily_update")
def run_daily_update() -> dict:
    return asyncio.run(_run_daily_update_async())
