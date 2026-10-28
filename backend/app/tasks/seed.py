import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.ecosystems.pypi.connector import PyPIConnector
from app.db import SessionLocal
from app.fetcher.pipeline import run_sync_pipeline
from app.models.dependency import Dependency
from app.models.enums import DependencyType, SyncType
from app.models.package import Package
from app.models.version import PackageVersion
from app.normalizers.names import normalize_package_name
from app.services.metrics import observe_sync_duration
from app.services.sync_failures import clear_sync_failure, upsert_sync_failure
from app.services.sync_state import mark_sync_failed, mark_sync_progress, mark_sync_running, mark_sync_success

logger = logging.getLogger(__name__)


async def _unresolved_dependency_names(db, package_id) -> list[str]:
    # Only required dependencies: following extras (test, doc, ...) pulls in thousands of unrelated packages.
    rows = (
        await db.scalars(
            select(Dependency.dependency_name).distinct()
            .join(PackageVersion, PackageVersion.id == Dependency.version_id)
            .where(
                PackageVersion.package_id == package_id,
                Dependency.dependency_ecosystem == "pypi",
                Dependency.dependency_type == DependencyType.runtime,
                Dependency.resolved_package_id.is_(None),
            )
        )
    ).all()
    return [normalize_package_name(name) for name in rows if name]


async def _run_seed_async(packages: list[str], max_depth: int = 10) -> dict[str, int | str]:
    started = perf_counter()
    connector = PyPIConnector()
    queue: deque[tuple[str, int]] = deque((normalize_package_name(name), 0) for name in packages)
    visited: set[str] = set()

    processed = 0
    failed = 0

    async with SessionLocal() as db:
        await mark_sync_running(db, SyncType.full, packages_total=len(queue))
        await db.commit()

        while queue:
            name, depth = queue.popleft()
            if name in visited:
                continue
            visited.add(name)

            try:
                metadata = await connector.fetch_package_metadata(name)
                package = await run_sync_pipeline(metadata, db)
                await clear_sync_failure(db, package_name=name)
                processed += 1

                if depth < max_depth:
                    unresolved = await _unresolved_dependency_names(db, package.id)
                    for dep_name in unresolved:
                        if dep_name not in visited:
                            queue.append((dep_name, depth + 1))

            except PackageNotFound as exc:
                failed += 1
                logger.warning("Seed package not found: package=%s depth=%s", name, depth)
                elapsed_ms = (perf_counter() - started) * 1000
                await db.rollback()
                await upsert_sync_failure(
                    db,
                    operation="seed",
                    package_name=name,
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
                logger.exception("Seed processing failed: package=%s depth=%s", name, depth)
                elapsed_ms = (perf_counter() - started) * 1000
                await db.rollback()
                await upsert_sync_failure(
                    db,
                    operation="seed",
                    package_name=name,
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


@celery_app.task(name="app.tasks.seed.run_seed")
def run_seed(packages: list[str] | None = None) -> dict[str, int | str]:
    settings = get_settings()
    seed_packages = packages or settings.seed_package_list
    return asyncio.run(_run_seed_async(seed_packages))
