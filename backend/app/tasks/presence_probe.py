"""Weekly presence probe (SPEC-MULTI-ECOSYSTEM.md §6.6): packages of the catalog that appeared in a source of the
default profile after we first saw them. The daily update only follows the sources a package is already known in."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter

import httpx
from sqlalchemy import select

from app.celery_app import celery_app
from app.connectors.base import PackageNotFound
from app.db import loop_local_session
from app.ecosystems.base import EcosystemPlugin, PackageRef, RegistryConfig, SourceProfile
from app.fetcher.discovery import NegativeCache, degraded_registry_ids, record_source_failure
from app.fetcher.pipeline import plugin_for, sync_package
from app.fetcher.sources import default_profile
from app.models.enums import Ecosystem, PackageOrigin
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.run import Run
from app.services.runs import FAILED, PARTIAL, SUCCESS, finish_run, start_run
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure

logger = logging.getLogger(__name__)

OPERATION = "presence_probe"


@dataclass(frozen=True, slots=True)
class Target:
    """A catalog package and the sources it is already known in (any `missing_since`)."""

    ref: PackageRef
    known: frozenset[str] = frozenset()


@dataclass(slots=True)
class ProbeResult:
    checked: int = 0        # packages with at least one source to ask
    updated: int = 0        # packages that got a new source
    failed: int = 0
    per_registry: dict[str, dict[str, int]] = field(default_factory=dict)


async def catalog_targets(db, ecosystem: str) -> list[Target]:
    rows = (
        await db.execute(
            select(Package.id, Package.namespace, Package.normalized_name)
            .where(Package.ecosystem == Ecosystem(ecosystem))
            .order_by(Package.namespace, Package.normalized_name)
        )
    ).all()
    known: dict[object, set[str]] = {}
    links = await db.execute(
        select(PackageRegistry.package_id, PackageRegistry.registry_id).where(PackageRegistry.ecosystem == Ecosystem(ecosystem))
    )
    for package_id, registry_id in links.all():
        known.setdefault(package_id, set()).add(str(registry_id))
    return [
        Target(PackageRef(ecosystem=ecosystem, namespace=namespace or "", name=name),  # type: ignore[arg-type]
               frozenset(known.get(package_id, ())))
        for package_id, namespace, name in rows
    ]


async def probe_packages(
    db,
    plugin: EcosystemPlugin,
    profile: SourceProfile,
    targets: Sequence[Target],
    cache: NegativeCache,
    run_id,
) -> ProbeResult:
    """Ask the sources of `profile` that routing allows but the package is not known in, and re-read the package
    from the ones that have it (the pipeline emits `source_added`). One commit per package."""
    result = ProbeResult()
    # Degraded sources are not probed; one that fails during this run joins them.
    skipped = await degraded_registry_ids(db)

    for target in targets:
        ref = target.ref
        candidates = [r for r in plugin.sources_for(ref, profile) if r.id not in target.known and r.id not in skipped]
        if not candidates:
            continue
        result.checked += 1
        found = await _probe_sources(db, plugin, ref, candidates, cache, result, skipped)
        if not found:
            continue

        try:
            await sync_package(db, ref, origin=PackageOrigin.dependency, run_id=run_id, extra_registries=found)
            await clear_sync_failure(db, ref=ref, operation=OPERATION)
            await db.commit()
            result.updated += 1
        except Exception as exc:  # noqa: BLE001 - one bad package must not stop the probe
            result.failed += 1
            not_found = isinstance(exc, PackageNotFound)
            if not_found:
                logger.warning("Presence probe: %s listed by %s but not readable", ref.name, [r.slug for r in found])
            else:
                logger.exception("Presence probe failed: package=%s", ref.name)
            await db.rollback()
            await upsert_sync_failure(db, operation=OPERATION, ref=ref, exc=exc, retryable=False if not_found else None)
            await db.commit()
    return result


async def _probe_sources(
    db,
    plugin: EcosystemPlugin,
    ref: PackageRef,
    candidates: Sequence[RegistryConfig],
    cache: NegativeCache,
    result: ProbeResult,
    skipped: set[str],
) -> list[RegistryConfig]:
    purl = plugin.coordinates.to_purl(ref)
    found = []
    for registry in candidates:
        if await cache.is_absent(registry, purl):
            continue
        stats = result.per_registry.setdefault(registry.slug, {"probed": 0, "found": 0, "errors": 0})
        stats["probed"] += 1
        try:
            present = await plugin.adapter(registry).exists(ref)
        except httpx.HTTPError as exc:
            stats["errors"] += 1
            await record_source_failure(db, registry, exc)
            skipped.add(registry.id)
            continue
        except Exception:  # noqa: BLE001 - an adapter bug would repeat for every package: stop asking this source
            stats["errors"] += 1
            logger.exception("Presence probe: source %s failed on %s", registry.slug, purl)
            skipped.add(registry.id)
            continue
        if present:
            stats["found"] += 1
            found.append(registry)
        else:
            await cache.remember_absent(registry, purl)
    return found


async def _run_presence_probe_async(ecosystem: str = "pypi") -> dict[str, object]:
    started = perf_counter()
    plugin = plugin_for(ecosystem)

    async with loop_local_session() as db:
        run = await start_run(db, OPERATION, ecosystem)
        if run is None:
            logger.info("Presence probe of %s skipped: a run is already going", ecosystem)
            return {"status": "skipped", "operation": OPERATION, "ecosystem": ecosystem,
                    "reason": "a run is already going"}
        run_id = run.id
        await db.commit()

        total = 0
        try:
            profile = await default_profile(db, ecosystem)
            targets = await catalog_targets(db, ecosystem)
            total = len(targets)
            async with NegativeCache.open() as cache:
                result = await probe_packages(db, plugin, profile, targets, cache, run_id)
        except Exception as exc:
            await db.rollback()
            await _finish(db, run_id, FAILED, ProbeResult(), total, error=f"{type(exc).__name__}: {exc}")
            raise

        await _finish(db, run_id, PARTIAL if result.failed else SUCCESS, result, total)

    return {
        "status": "completed",
        "operation": OPERATION,
        "ecosystem": ecosystem,
        "packages_total": total,
        "packages_checked": result.checked,
        "packages_updated": result.updated,
        "packages_failed": result.failed,
        "per_registry": result.per_registry,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _finish(db, run_id, status: str, result: ProbeResult, total: int, error: str | None = None) -> None:
    # The run object may have been expired by a rollback; load it again before closing.
    run = await db.get(Run, run_id)
    await finish_run(db, run, status, error=error, packages_total=total, packages_checked=result.checked,
                     packages_updated=result.updated, packages_failed=result.failed, per_registry=result.per_registry)
    await db.commit()


@celery_app.task(name="app.tasks.presence_probe.run_presence_probe")
def run_presence_probe(ecosystem: str = "pypi") -> dict[str, object]:
    return asyncio.run(_run_presence_probe_async(ecosystem))
