"""Release dates of Maven versions in the background (SPEC-MULTI-ECOSYSTEM.md §7.1, like the crates.io API fields,
Q22).

A Maven repository dates a version only by the `Last-Modified` of its POM: one HEAD per version. A fetch dates just the
latest version (its POM is read anyway); this task, run every minute, dates up to a batch of the others, newest
versions of the most recently synced packages first. A version whose POM has no date is marked in raw_metadata and not
asked again until the package is synced again; one the source fails for stays unmarked and is tried at the next run.
"""

import asyncio
import logging

from sqlalchemy import not_, select

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


async def _run(limit: int) -> dict[str, object]:
    plugin = plugin_for("maven")
    counts = {"dated": 0, "undated": 0, "failed": 0}
    async with loop_local_session() as db:
        rows = (await db.execute(
            select(PackageVersion, Package, Registry)
            .join(Package, Package.id == PackageVersion.package_id)
            .join(VersionSource, VersionSource.version_id == PackageVersion.id)
            .join(Registry, Registry.id == VersionSource.registry_id)
            .where(Package.ecosystem == Ecosystem.maven, Registry.enabled, PackageVersion.release_date.is_(None),
                   not_(PackageVersion.raw_metadata.has_key(UNKNOWN)))
            .order_by(Package.synced_at.desc(), PackageVersion.sort_key.desc(), Registry.priority)
            .limit(limit)
        )).all()
        semaphore = asyncio.Semaphore(get_settings().daily_update_concurrency)
        seen: set[object] = set()

        async def date(version: PackageVersion, package: Package, registry: Registry) -> None:
            adapter = plugin.adapter(registry_config(registry))
            ref = PackageRef("maven", package.namespace or "", package.normalized_name)
            try:
                async with semaphore:
                    found = await adapter.release_date(ref, version.version)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - one failing version must not stop the batch
                logger.warning("No release date for %s@%s in %s", package.name, version.version, registry.slug,
                               exc_info=True)
                counts["failed"] += 1
                return
            if found is None:
                version.raw_metadata = {**(version.raw_metadata or {}), UNKNOWN: True}
                counts["undated"] += 1
            else:
                version.release_date = found
                counts["dated"] += 1

        jobs = []
        for version, package, registry in rows:
            if version.id not in seen:               # the first (highest-priority) source that has it
                seen.add(version.id)
                jobs.append(date(version, package, registry))
        await asyncio.gather(*jobs)
        await db.commit()
    return {"status": "completed", "operation": "maven_dates", **counts}


@celery_app.task(name="app.tasks.maven_dates.fill_maven_release_dates")
def fill_maven_release_dates(limit: int | None = None) -> dict[str, object]:
    return asyncio.run(_run(limit or get_settings().maven_dates_batch))
