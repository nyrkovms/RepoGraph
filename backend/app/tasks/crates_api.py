"""Descriptions, links and licenses of crates from the crates.io API, in the background (SPEC-MULTI-ECOSYSTEM.md §7.1,
Q22).

The index gives a crate everything a resolve needs; the API (1 request/s for everyone) only the package-level fields.
A crate added or changed since its last API read has no `api_fetched_at` in packages.raw_metadata, which every sync
rewrites from the index; this task, run every minute, reads up to a batch of them, newest syncs first, and marks them.
A crate the API fails for stays unmarked and is tried at the next run.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import not_, select

from app.celery_app import celery_app
from app.config import get_settings
from app.db import loop_local_session
from app.ecosystems.base import NormalizedPackage, PackageRef
from app.fetcher.pipeline import plugin_for
from app.fetcher.sources import registry_config
from app.models.enums import Ecosystem
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry

logger = logging.getLogger(__name__)

MARK = "api_fetched_at"
FIELDS = ("description", "homepage_url", "repository_url", "documentation_url", "license_raw", "license_spdx",
          "keywords", "downloads_total", "downloads_recent")


async def _run(limit: int) -> dict[str, object]:
    plugin = plugin_for("crates")
    counts = {"filled": 0, "failed": 0}
    async with loop_local_session() as db:
        rows = (await db.execute(
            select(Package, Registry)
            .join(PackageRegistry, PackageRegistry.package_id == Package.id)
            .join(Registry, Registry.id == PackageRegistry.registry_id)
            .where(Package.ecosystem == Ecosystem.crates, Registry.enabled, Registry.config.has_key("api_url"),
                   PackageRegistry.missing_since.is_(None), not_(Package.raw_metadata.has_key(MARK)))
            .order_by(Package.synced_at.desc(), Registry.priority)
            .limit(limit)
        )).all()
        done: set[object] = set()
        for package, registry in rows:
            if package.id in done:
                continue
            adapter = plugin.adapter(registry_config(registry))
            fetch = getattr(adapter, "fetch_api_fields", None)
            if fetch is None:
                continue
            shell = NormalizedPackage(ref=PackageRef("crates", "", package.normalized_name), registry_id=str(registry.id),
                                      display_name=package.name, revision=None, versions=[])
            if not await fetch(shell):
                counts["failed"] += 1
                continue
            for name in FIELDS:
                value = getattr(shell, name)
                if value is not None:
                    setattr(package, name, value)
            package.raw_metadata = {**(package.raw_metadata or {}), MARK: datetime.now(UTC).isoformat()}
            done.add(package.id)
            counts["filled"] += 1
            await db.commit()
    return {"status": "completed", "operation": "crates_api", **counts}


@celery_app.task(name="app.tasks.crates_api.fill_crates_api_fields")
def fill_crates_api_fields(limit: int | None = None) -> dict[str, object]:
    return asyncio.run(_run(limit or get_settings().crates_api_batch))
