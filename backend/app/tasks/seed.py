import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.fetcher.pipeline import load_builtin_plugins, pypi_ref, sync_package
from app.models.dependency import Dependency
from app.models.enums import DependencyType, PackageOrigin, SyncType
from app.models.package import Package
from app.models.run import Run
from app.models.version import PackageVersion
from app.services.metrics import observe_sync_duration
from app.services.runs import FAILED, PARTIAL, SUCCESS, finish_run, start_run
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success
from app.tasks.fetch_packages import ref_from_target

logger = logging.getLogger(__name__)


async def _unresolved_dependency_refs(db, package_id) -> list[PackageRef]:
    # Only required dependencies: following extras (test, doc, ...) pulls in thousands of unrelated packages.
    # Only ecosystems we have a plugin for: the rest could not be fetched anyway.
    ecosystems = [plugin.ecosystem for plugin in load_builtin_plugins()]
    rows = (
        await db.execute(
            select(Dependency.dependency_ecosystem, Dependency.dependency_namespace, Dependency.dependency_name).distinct()
            .join(PackageVersion, PackageVersion.id == Dependency.version_id)
            .where(
                PackageVersion.package_id == package_id,
                Dependency.dependency_ecosystem.in_(ecosystems),
                Dependency.dependency_type == DependencyType.runtime,
                Dependency.variant_registry_id.is_(None),
                Dependency.resolved_package_id.is_(None),
            )
        )
    ).all()
    refs = []
    for ecosystem, namespace, name in rows:
        if not name:
            continue
        # PyPI names are normalized by the plugin; other ecosystems store them normalized already.
        refs.append(pypi_ref(name) if ecosystem.value == "pypi" else PackageRef(ecosystem.value, namespace or "", name))
    return refs


async def _run_seed_async(packages: list[str], max_depth: int = 10) -> dict[str, int | str]:
    """Seed `packages` (purls, or bare PyPI names) and, up to `max_depth`, their required dependencies."""
    started = perf_counter()
    queue: deque[tuple[PackageRef, int]] = deque()
    visited: set[PackageRef] = set()
    processed = 0
    failed = 0

    for raw in packages:
        try:
            queue.append((ref_from_target(raw), 0))
        except ValueError as exc:
            failed += 1
            logger.warning("Seed skipped, bad package %r: %s", raw, exc)

    async with loop_local_session() as db:
        run = await start_run(db, "seed", None, exclusive=False)
        assert run is not None
        run_id = run.id
        await mark_sync_running(db, SyncType.full, packages_total=len(queue))
        await db.commit()

        try:
            while queue:
                ref, depth = queue.popleft()
                if ref in visited:
                    continue
                visited.add(ref)

                try:
                    origin = PackageOrigin.seed if depth == 0 else PackageOrigin.dependency
                    package = await sync_package(db, ref, origin=origin, run_id=run_id)
                    await clear_sync_failure(db, ref=ref)
                    processed += 1

                    if depth < max_depth:
                        for dependency in await _unresolved_dependency_refs(db, package.id):
                            if dependency not in visited:
                                queue.append((dependency, depth + 1))

                except PackageNotFound as exc:
                    failed += 1
                    logger.warning("Seed package not found: package=%s depth=%s", ref.name, depth)
                    elapsed_ms = (perf_counter() - started) * 1000
                    await db.rollback()
                    await upsert_sync_failure(
                        db,
                        operation="seed",
                        ref=ref,
                        exc=exc,
                        retryable=False,
                    )
                    await mark_sync_failed(
                        db,
                        str(exc),
                        processed=processed,
                        total=processed + len(queue),
                        duration_ms=elapsed_ms,
                        operation="seed",
                    )
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.exception("Seed processing failed: package=%s depth=%s", ref.name, depth)
                    elapsed_ms = (perf_counter() - started) * 1000
                    await db.rollback()
                    await upsert_sync_failure(
                        db,
                        operation="seed",
                        ref=ref,
                        exc=exc,
                    )
                    await mark_sync_failed(
                        db,
                        str(exc),
                        processed=processed,
                        total=processed + len(queue),
                        duration_ms=elapsed_ms,
                        operation="seed",
                    )
                    await db.commit()
                    continue

                await mark_sync_progress(db, processed=processed)
                sync_state = await mark_sync_running(db, SyncType.full, packages_total=processed + len(queue))
                sync_state.packages_synced = processed
                await db.commit()

            elapsed_ms = (perf_counter() - started) * 1000
            await mark_sync_success(
                db,
                processed=processed,
                total=processed,
                duration_ms=elapsed_ms,
                operation="seed",
            )
            await db.commit()
            observe_sync_duration("seed", elapsed_ms / 1000.0)
        except Exception as exc:
            await db.rollback()
            await _finish(db, run_id, FAILED, processed, failed, error=f"{type(exc).__name__}: {exc}")
            raise

        status = SUCCESS if failed == 0 else PARTIAL if processed else FAILED
        await _finish(db, run_id, status, processed, failed)

        package_count = await db.scalar(select(func.count(Package.id)).where(Package.ecosystem == "pypi"))
        version_count = await db.scalar(select(func.count(PackageVersion.id)))

    return {
        "status": "completed",
        "operation": "seed",
        "processed": processed,
        "failed": failed,
        "packages_total": int(package_count or 0),
        "versions_total": int(version_count or 0),
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _finish(db, run_id, status: str, processed: int, failed: int, error: str | None = None) -> None:
    # The run object was expired by a rollback; load it again before closing.
    run = await db.get(Run, run_id)
    await finish_run(db, run, status, error=error, packages_total=processed + failed, packages_updated=processed,
                     packages_failed=failed)
    await db.commit()


@celery_app.task(name="app.tasks.seed.run_seed")
def run_seed(packages: list[str] | None = None) -> dict[str, int | str]:
    settings = get_settings()
    seed_packages = packages or settings.seed_package_list
    return asyncio.run(_run_seed_async(seed_packages))
