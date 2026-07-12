"""Classification tasks: per-version (after probe) and batch (re)classification."""

import logging
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import exists, select

from app.celery_app import celery_app
from app.classifier import CLASSIFIER_VERSION
from app.classifier.service import classify_one_version, seed_rule_registries
from app.db import SessionLocal
from app.models.classification import Classification
from app.models.dist_probe import DistProbe
from app.models.package import Package
from app.models.version import PackageVersion
from app.services.metrics import observe_sync_duration
from app.tasks.runner import run_async

logger = logging.getLogger(__name__)


async def _classify_version_async(version_id: str, force: bool = False) -> dict[str, str]:
    async with SessionLocal() as db:
        version = await db.get(PackageVersion, version_id)
        if version is None:
            return {"status": "skipped", "reason": "version not found"}
        package = await db.get(Package, version.package_id)
        if package is None:
            return {"status": "skipped", "reason": "package not found"}

        if not force:
            already = await db.scalar(
                select(
                    exists().where(
                        Classification.version_id == version.id,
                        Classification.classifier_version == CLASSIFIER_VERSION,
                    )
                )
            )
            if already:
                return {"status": "skipped", "reason": "already classified"}

        await seed_rule_registries(db)
        result = await classify_one_version(db, version, package)
        await db.commit()
        return {
            "status": "completed",
            "package": package.normalized_name,
            "version": version.version,
            "classification_status": result.status,
            "roles": sorted(result.roles),
        }


@celery_app.task(name="app.tasks.classify.classify_version")
def classify_version(version_id: str, force: bool = False) -> dict[str, str]:
    return run_async(_classify_version_async(version_id, force))


async def _run_classification_async(reclassify: bool = False, limit: int | None = None) -> dict:
    """Batch: classify every version that has a representative probe row
    (created automatically for each package's latest) and lacks a verdict of
    the current CLASSIFIER_VERSION. --reclassify recomputes current version rows."""
    started = perf_counter()
    classified = 0
    by_status: dict[str, int] = {}

    async with SessionLocal() as db:
        await seed_rule_registries(db)
        query = (
            select(PackageVersion, Package)
            .join(Package, Package.id == PackageVersion.package_id)
            .where(
                exists().where(
                    DistProbe.version_id == PackageVersion.id, DistProbe.is_representative.is_(True)
                )
            )
            .order_by(PackageVersion.id)
        )
        if not reclassify:
            query = query.where(
                ~exists().where(
                    Classification.version_id == PackageVersion.id,
                    Classification.classifier_version == CLASSIFIER_VERSION,
                )
            )
        if limit:
            query = query.limit(limit)

        rows = (await db.execute(query)).all()
        for version, package in rows:
            try:
                result = await classify_one_version(db, version, package)
                await db.commit()
                classified += 1
                by_status[result.status] = by_status.get(result.status, 0) + 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Classification failed: package=%s version=%s", package.normalized_name, version.version
                )
                await db.rollback()

    observe_sync_duration("classify", perf_counter() - started)
    return {
        "status": "completed",
        "operation": "classify",
        "classifier_version": CLASSIFIER_VERSION,
        "classified": classified,
        "by_status": by_status,
        "reclassify": reclassify,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "finished_at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name="app.tasks.classify.run_classification")
def run_classification(reclassify: bool = False, limit: int | None = None) -> dict:
    return run_async(_run_classification_async(reclassify=reclassify, limit=limit))
