import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.ecosystems.purl import parse as parse_purl
from app.ecosystems.registry import registered
from app.fetcher import sources
from app.fetcher.pipeline import load_builtin_plugins, plugin_for, sync_package
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


def ref_from_target(raw: str) -> PackageRef:
    """A purl, or a bare name, which is a PyPI project name (IMPL T9). Raises ValueError with a readable reason."""
    raw = raw.strip()
    if raw.startswith("pkg:"):
        return ref_from_purl(raw)
    return plugin_for("pypi").coordinates.parse(raw)


def invalid_purls(purls: list[str]) -> dict[str, str]:
    errors = {}
    for purl in purls:
        try:
            ref_from_purl(purl)
        except ValueError as exc:
            errors[purl] = str(exc)
    return errors


async def canonical_ref(db: AsyncSession, ref: PackageRef) -> PackageRef | None:
    """The package under the spelling its sources publish, for input in another one (crates: `serde-json` is
    `serde_json`, SPEC Q21); None if no source of the default profile can tell."""
    plugin = plugin_for(ref.ecosystem)
    for registry in plugin.sources_for(ref, await sources.default_profile(db, ref.ecosystem)):
        lookup = getattr(plugin.adapter(registry), "canonical_name", None)
        name = await lookup(ref.name) if lookup is not None else None
        if isinstance(name, str) and name != ref.name:
            return replace(ref, name=name)
    return None


async def sync_manual(db: AsyncSession, ref: PackageRef, origin: PackageOrigin = PackageOrigin.manual) -> PackageRef:
    """sync_package for a package someone asked for by name; returns the ref it was stored under."""
    try:
        await sync_package(db, ref, origin=origin)
        return ref
    except PackageNotFound:
        await db.rollback()
        canonical = await canonical_ref(db, ref)
        if canonical is None:
            raise
    await sync_package(db, canonical, origin=origin)
    return canonical


async def _fetch_packages_async(purls: list[str], origin: PackageOrigin = PackageOrigin.manual) -> dict[str, object]:
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
            try:
                stored = await sync_manual(db, ref, origin)
                await clear_sync_failure(db, ref=ref)
                if stored != ref:
                    logger.info("Fetched %s as %s", purl, stored.name)
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
                await upsert_sync_failure(db, operation="fetch", ref=ref, exc=exc, retryable=False if not_found else None)
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
def fetch_packages(purls: list[str], origin: str = "manual") -> dict[str, object]:
    """`origin` `discovered`: packages a resolve found (RPM, SPEC §9.8.6), not asked for by anyone."""
    return asyncio.run(_fetch_packages_async(purls, PackageOrigin(origin)))
