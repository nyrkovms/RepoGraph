"""Release dates of versions in the background (SPEC-MULTI-ECOSYSTEM.md §7.1, like the crates.io API fields,
Q22).

A Maven repository dates a version only by the `Last-Modified` of its POM (one HEAD per version), a Go proxy by its
`.info` (one GET per version). A fetch dates just the latest version (and Go's pseudo-versions, which carry their
time); this task, run every minute, dates up to a batch of the others, newest versions of the most recently synced
packages first, each from the highest-priority enabled source that has it (its adapter's `release_date`). A
version whose POM has no date is marked and not asked again; one the source fails for (down, or refusing HEAD) is
marked with the time and tried again a day later, so failing versions do not take every batch.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, not_, or_, select

from app.celery_app import celery_app
from app.config import get_settings
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.fetcher.pipeline import plugin_for
from app.fetcher.sources import registry_config
from app.models.enums import Ecosystem
from app.models.package import Package
from app.models.registry import Registry
from app.models.version import PackageVersion, VersionSource

logger = logging.getLogger(__name__)

UNKNOWN = "release_date_unknown"
FAILED = "release_date_failed_at"
RETRY_AFTER = timedelta(days=1)
ECOSYSTEMS = (Ecosystem.maven, Ecosystem.go)


async def _run(limit: int, ecosystems: tuple[Ecosystem, ...] = ECOSYSTEMS) -> dict[str, object]:
    counts = {"dated": 0, "undated": 0, "failed": 0}
    now = datetime.now(UTC)
    cutoff = (now - RETRY_AFTER).isoformat()
    async with loop_local_session() as db:
        enabled_source = (select(VersionSource.version_id).join(Registry, Registry.id == VersionSource.registry_id)
                          .where(VersionSource.version_id == PackageVersion.id, Registry.enabled))
        rows = (await db.execute(
            select(PackageVersion, Package)
            .join(Package, Package.id == PackageVersion.package_id)
            .where(Package.ecosystem.in_(ecosystems), PackageVersion.release_date.is_(None),
                   not_(PackageVersion.raw_metadata.has_key(UNKNOWN)),
                   or_(not_(PackageVersion.raw_metadata.has_key(FAILED)),
                       PackageVersion.raw_metadata[FAILED].astext < cutoff),
                   exists(enabled_source))
            .order_by(Package.synced_at.desc().nulls_last(), PackageVersion.sort_key.desc())
            .limit(limit)
        )).all()
        sources = {}
        for version, _ in rows:
            sources[version.id] = await db.scalar(
                select(Registry).join(VersionSource, VersionSource.registry_id == Registry.id)
                .where(VersionSource.version_id == version.id, Registry.enabled).order_by(Registry.priority).limit(1))
        semaphore = asyncio.Semaphore(get_settings().daily_update_concurrency)

        async def date(version: PackageVersion, package: Package, registry: Registry) -> None:
            adapter = plugin_for(package.ecosystem.value).adapter(registry_config(registry))
            ref = PackageRef(package.ecosystem.value, package.namespace or "", package.normalized_name)
            try:
                async with semaphore:
                    found = await adapter.release_date(ref, version.version)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - one failing version must not stop the batch
                logger.warning("No release date for %s@%s in %s", package.name, version.version, registry.slug,
                               exc_info=True)
                version.raw_metadata = {**(version.raw_metadata or {}), FAILED: now.isoformat()}
                counts["failed"] += 1
                return
            if found is None:
                version.raw_metadata = {**(version.raw_metadata or {}), UNKNOWN: True}
                counts["undated"] += 1
            else:
                version.release_date = found
                counts["dated"] += 1

        await asyncio.gather(*(date(version, package, sources[version.id]) for version, package in rows
                               if sources[version.id] is not None))
        await db.commit()
    return {"status": "completed", "operation": "release_dates", **counts}


@celery_app.task(name="app.tasks.release_dates.fill_release_dates")
def fill_release_dates(limit: int | None = None) -> dict[str, object]:
    return asyncio.run(_run(limit or get_settings().release_dates_batch))
