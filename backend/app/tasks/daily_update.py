"""Daily update (SPEC-DAILY-UPDATE.md, SPEC-MULTI-ECOSYSTEM.md §8). Per ecosystem: every enabled source finds what
changed with its own ChangeDetector; a package changed in any source is re-read from all its sources (§8.1)."""

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.db import loop_local_session, loop_local_sessionmaker
from app.ecosystems.base import CHANGED_SINCE, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.registry import registered
from app.fetcher import discovery, sources
from app.fetcher.pipeline import load_builtin_plugins, mark_unavailable, plugin_for, sync_package
from app.models.enums import Ecosystem, SyncStatus
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.services import runs
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure

logger = logging.getLogger(__name__)

OPERATION = "daily_update"
_OUTCOMES = ("updated", "stale", "unavailable", "failed")


@dataclass(slots=True)
class _Work:
    """A package to re-read, with the revision each source reported (None: the source no longer lists it)."""

    package_id: object
    ref: PackageRef
    expected: dict[str, str | None] = field(default_factory=dict)


async def _tracked(db: AsyncSession, registry: RegistryConfig) -> list[TrackedPackage]:
    rows = (
        await db.execute(
            select(Package.id, Package.ecosystem, Package.namespace, Package.normalized_name,
                   PackageRegistry.upstream_revision, PackageRegistry.etag, PackageRegistry.missing_since)
            .join(PackageRegistry, PackageRegistry.package_id == Package.id)
            .where(PackageRegistry.registry_id == registry.id)
        )
    ).all()
    return [
        TrackedPackage(
            ref=PackageRef(ecosystem=row.ecosystem.value, namespace=row.namespace or "", name=row.normalized_name),
            package_id=str(row.id), revision=row.upstream_revision, etag=row.etag,
            unavailable=row.missing_since is not None,   # already known to be gone from this source
        )
        for row in rows
    ]


def is_stale(expected: str | None, stored: str | None) -> bool:
    """A source answered with an older revision than its change feed announced: PyPI JSON and npm packuments are
    served through a CDN and may lag behind the feed right after a release. Numeric revisions (serials) compare by
    value; "not:<revision>" (a feed that only knows the package changed) is stale while the answer is still that."""
    if expected is None or stored is None:
        return False
    if expected.startswith(CHANGED_SINCE):
        return stored == expected[len(CHANGED_SINCE):]
    if not (expected.isdigit() and stored.isdigit()):
        return False
    return int(stored) < int(expected)


async def _update_package(make_session: Callable[[], AsyncSession], work: _Work, run_id) -> str:
    """Re-read one package from its sources and write it. Returns updated | stale | unavailable | failed."""
    async with make_session() as db:
        try:
            package = await sync_package(db, work.ref, run_id=run_id)
            await clear_sync_failure(db, ref=work.ref, operation=OPERATION)
            await db.commit()
        except PackageNotFound:
            # Gone from every source it was asked from.
            await db.rollback()
            existing = await db.get(Package, uuid.UUID(str(work.package_id)))
            newly = await mark_unavailable(db, existing, run_id) if existing else False
            await clear_sync_failure(db, ref=work.ref, operation=OPERATION)
            await db.commit()
            return "unavailable" if newly else "updated"
        except Exception as exc:  # noqa: BLE001 - also SourceDegraded: the package waits for its source
            logger.warning("Daily update failed: package=%s error=%s", work.ref.name, exc.__class__.__name__)
            await db.rollback()
            await upsert_sync_failure(db, operation=OPERATION, ref=work.ref, exc=exc)
            await db.commit()
            return "failed"

        stored = dict(
            (str(registry_id), revision)
            for registry_id, revision in (
                await db.execute(select(PackageRegistry.registry_id, PackageRegistry.upstream_revision)
                                 .where(PackageRegistry.package_id == package.id))
            ).all()
        )
    if any(is_stale(expected, stored.get(registry_id)) for registry_id, expected in work.expected.items()):
        return "stale"
    return "updated"


async def _update_many(make_session: Callable[[], AsyncSession], items: list[_Work], run_id,
                       concurrency: int) -> dict[str, list[_Work]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, list[_Work]] = {outcome: [] for outcome in _OUTCOMES}

    async def one(work: _Work) -> None:
        async with semaphore:
            results[await _update_package(make_session, work, run_id)].append(work)

    await asyncio.gather(*(one(work) for work in items))
    return results


async def _detect(db: AsyncSession, registries: list[RegistryConfig]) -> tuple[dict[object, _Work], dict[str, dict], dict[str, Mapping[str, object]], int]:
    """Ask every source's change detector. Returns the work, per-source details for the run, new cursors, and the
    number of distinct packages tracked."""
    degraded = await discovery.degraded_registry_ids(db)
    cursors = {
        str(state.registry_id): state.cursor_data or {}
        for state in (await db.scalars(select(SyncState).where(SyncState.registry_id.in_([r.id for r in registries])))).all()
    }
    work: dict[object, _Work] = {}
    per_registry: dict[str, dict] = {}
    new_cursors: dict[str, Mapping[str, object]] = {}
    checked: set[str] = set()
    for registry in registries:
        if registry.id in degraded:
            logger.warning("Daily update: source %s is degraded, skipped", registry.slug)
            per_registry[registry.slug] = {"status": "degraded"}
            continue
        tracked = await _tracked(db, registry)
        checked.update(package.package_id for package in tracked)
        try:
            plugin = plugin_for(registry.ecosystem)
            changes = await plugin.adapter(registry).change_detector().detect(tracked, cursors.get(registry.id, {}))
        except Exception as exc:  # noqa: BLE001 - one source failing does not stop the others
            logger.exception("Daily update: change detection failed for %s", registry.slug)
            if isinstance(exc, httpx.HTTPError):
                await discovery.record_source_failure(db, registry, exc)
            per_registry[registry.slug] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}
            continue

        for package, revision in changes.changed:
            work.setdefault(package.package_id, _Work(package.package_id, package.ref)).expected[registry.id] = revision
        for package in changes.missing:
            work.setdefault(package.package_id, _Work(package.package_id, package.ref)).expected[registry.id] = None
        if changes.reconcile_all:
            # The feed was lost (npm seq reset): every package of the source is checked.
            for package in tracked:
                work.setdefault(package.package_id, _Work(package.package_id, package.ref))
        new_cursors[registry.id] = changes.cursor
        per_registry[registry.slug] = {
            "status": "ok", "tracked": len(tracked), "changed": len(changes.changed), "missing": len(changes.missing),
            "cursor": dict(changes.cursor),
        }
    return work, per_registry, new_cursors, len(checked)


async def _save_cursors(db: AsyncSession, cursors: Mapping[str, Mapping[str, object]]) -> None:
    for registry_id, cursor in cursors.items():
        stmt = insert(SyncState).values(registry_id=registry_id, status=SyncStatus.idle, cursor_data=dict(cursor))
        await db.execute(stmt.on_conflict_do_update(index_elements=["registry_id"], set_={"cursor_data": dict(cursor)}))


async def run_ecosystem(ecosystem: str) -> dict:
    settings = get_settings()
    started = perf_counter()
    concurrency = max(1, settings.daily_update_concurrency)

    async with loop_local_session() as db:
        run = await runs.start_run(db, OPERATION, ecosystem)
        if run is None:
            running = await runs.running_run(db, OPERATION, ecosystem)
            logger.warning("Daily update of %s skipped: run %s is still running", ecosystem, running.id if running else "?")
            return {"status": "skipped", "operation": OPERATION, "ecosystem": ecosystem,
                    "running_run_id": str(running.id) if running else None}
        run_id = run.id
        await db.commit()
        registries = await sources.enabled_registries(db, ecosystem)

    results: dict[str, list[_Work]] = {outcome: [] for outcome in _OUTCOMES}
    work: dict[object, _Work] = {}
    per_registry: dict[str, dict] = {}
    cursors: dict[str, Mapping[str, object]] = {}
    checked = 0
    error = None
    try:
        async with loop_local_session() as db:
            work, per_registry, cursors, checked = await _detect(db, registries)
        logger.info("Daily update %s: checked=%s changed=%s", ecosystem, checked, len(work))

        async with loop_local_sessionmaker(pool_size=concurrency) as make_session:
            results = await _update_many(make_session, list(work.values()), run_id, concurrency)
            if results["stale"]:
                await asyncio.sleep(settings.daily_update_stale_retry_seconds)
                retry = await _update_many(make_session, results["stale"], run_id, concurrency)
                if retry["stale"]:
                    logger.warning("Daily update: %s packages still stale, next run picks them up", len(retry["stale"]))
                results["stale"] = retry["stale"]
                for outcome in ("updated", "unavailable", "failed"):
                    results[outcome].extend(retry[outcome])

        sources_ok = all(details["status"] == "ok" for details in per_registry.values())
        status = runs.SUCCESS if not results["failed"] and sources_ok else runs.PARTIAL
    except Exception as exc:  # noqa: BLE001
        logger.exception("Daily update of %s failed", ecosystem)
        status = runs.FAILED
        error = f"{exc.__class__.__name__}: {exc}"

    counts = {
        "packages_total": checked,
        "packages_checked": checked,
        "packages_changed": len(work),
        "packages_updated": len(results["updated"]) + len(results["stale"]),
        "packages_failed": len(results["failed"]),
        "packages_unavailable": len(results["unavailable"]),
    }
    index_serial = next((d.get("cursor", {}).get("index_serial") for d in per_registry.values() if "index_serial" in d.get("cursor", {})), None)
    async with loop_local_session() as db:
        run = await db.get(Run, run_id)
        events = await runs.finish_run(db, run, status, error=error, per_registry=per_registry, **counts)
        if status != runs.FAILED:
            await _save_cursors(db, cursors)
        await db.commit()
        finished_at = run.finished_at

    summary = {
        "status": status,
        "operation": OPERATION,
        "ecosystem": ecosystem,
        "run_id": str(run_id),
        "index_serial": index_serial,
        "checked": counts["packages_checked"],
        "changed": counts["packages_changed"],
        "updated": counts["packages_updated"],
        "failed": counts["packages_failed"],
        "unavailable": counts["packages_unavailable"],
        "events": events,
        "per_registry": per_registry,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": finished_at.isoformat(),
    }
    _record_metrics(summary)
    return summary


async def _ecosystems_to_update() -> list[str]:
    """Ecosystems with a plugin and at least one enabled source, in enum order."""
    load_builtin_plugins()
    with_plugin = {plugin.ecosystem for plugin in registered()}
    async with loop_local_session() as db:
        return [e.value for e in Ecosystem if e.value in with_plugin and await sources.enabled_registries(db, e.value)]


async def _run_daily_update_async(ecosystem: str | None = None) -> dict:
    """One ecosystem, or every ecosystem in turn (each its own run, guarded against overlap separately)."""
    if ecosystem is not None:
        return await run_ecosystem(ecosystem)
    summaries = [await run_ecosystem(e) for e in await _ecosystems_to_update()]
    if len(summaries) == 1:
        return summaries[0]
    worst = next((s for s in (runs.FAILED, runs.PARTIAL, "skipped") if any(x["status"] == s for x in summaries)), runs.SUCCESS)
    return {"status": worst, "operation": OPERATION, "runs": summaries}


def _record_metrics(summary: dict) -> None:
    from app.services.metrics import inc_package_events, record_daily_update_run

    record_daily_update_run(
        status=summary["status"],
        duration_seconds=summary["duration_ms"] / 1000.0,
        updated=summary["updated"],
        failed=summary["failed"],
        unavailable=summary["unavailable"],
        finished_at_ts=datetime.fromisoformat(summary["finished_at"]).timestamp(),
        ecosystem=summary["ecosystem"],
    )
    for kind, count in summary["events"].items():
        inc_package_events(kind, count, ecosystem=summary["ecosystem"])


@celery_app.task(name="app.tasks.daily_update.run_daily_update")
def run_daily_update(ecosystem: str | None = None) -> dict:
    return asyncio.run(_run_daily_update_async(ecosystem))

