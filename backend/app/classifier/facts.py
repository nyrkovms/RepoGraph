"""On-demand facts service (spec classification / on-demand-classification).

The proxy's check system calls PackageHub synchronously: ensure the latest
version's facts exist (lazy fetch -> inline probe -> inline classify) and
return them in one response. Facts only — no allow/deny verdicts: decisions
belong to the calling check system.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier.effective import classification_block, current_classification, registry_presence
from app.connectors.factory import build_connector, registry_info_from_row
from app.fetcher.lazy import LazyFetcher
from app.fetcher.pipeline import find_package, get_or_create_default_registry, run_sync_pipeline
from app.models.dist_probe import DistProbe
from app.models.package import Package
from app.models.version import PackageVersion
from app.normalizers.names import normalize_name

logger = logging.getLogger(__name__)


class FactsUnavailable(Exception):
    """Typed failure of the facts service (fail-fast, no partial answer)."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind  # not_found | auth_error | upstream_error
        self.detail = detail


async def _latest_version_row(db: AsyncSession, package: Package) -> PackageVersion | None:
    return await db.scalar(
        select(PackageVersion).where(
            PackageVersion.package_id == package.id,
            PackageVersion.version == package.latest_version,
        )
    )


async def _ensure_package(db: AsyncSession, ecosystem: str, name: str, refresh: bool) -> Package:
    normalized = normalize_name(ecosystem, name)
    package = await find_package(db, ecosystem, normalized)

    if package is None or refresh:
        registry = await get_or_create_default_registry(db, ecosystem)
        connector = build_connector(registry_info_from_row(registry))
        if package is None:
            fetcher = LazyFetcher(connector=connector, db=db, ecosystem=ecosystem)
            result = await fetcher.ensure_package_result(normalized)
            if result.status == "not_found":
                raise FactsUnavailable("not_found", result.error or "package not found upstream")
            if result.status == "auth_error":
                raise FactsUnavailable("auth_error", result.error or "registry rejected credentials")
            if result.status != "ok" or result.package is None:
                raise FactsUnavailable("upstream_error", result.error or "upstream fetch failed")
            package = result.package
        else:
            metadata = await connector.fetch_package_metadata(normalized)
            package = await run_sync_pipeline(metadata, db, registry=registry)
    return package


async def _ensure_probe(db: AsyncSession, version: PackageVersion) -> None:
    existing = await db.scalar(
        select(DistProbe).where(DistProbe.version_id == version.id, DistProbe.is_representative.is_(True))
    )
    if existing is not None:
        return
    # Та же корутина, что у Celery-задачи: Redis-лок и идемпотентные вставки
    # защищают от гонки с воркером; retryable-сбой → fail-fast наружу.
    from app.tasks.probe import RetryableProbeError, _probe_version_async

    try:
        result = await _probe_version_async(str(version.id))
    except RetryableProbeError as exc:
        raise FactsUnavailable("upstream_error", f"probe failed: {exc}") from exc
    if result.get("status") == "failed":
        raise FactsUnavailable("upstream_error", str(result.get("reason") or "probe failed"))
    if result.get("reason") == "locked":
        # воркер уже качает этот артефакт; перечитаем результат ниже —
        # если не успел, честно откажем как транзиентный сбой
        existing = await db.scalar(
            select(DistProbe).where(DistProbe.version_id == version.id, DistProbe.is_representative.is_(True))
        )
        if existing is None:
            raise FactsUnavailable("upstream_error", "probe in progress elsewhere; retry shortly")


async def _ensure_classification(db: AsyncSession, version: PackageVersion, package: Package) -> None:
    if await current_classification(db, version.id) is not None:
        return
    from app.classifier.service import classify_one_version, seed_rule_registries

    await seed_rule_registries(db)
    await classify_one_version(db, version, package)
    await db.commit()


async def ensure_facts(db: AsyncSession, ecosystem: str, name: str, refresh: bool = False) -> dict:
    """Facts for the package's latest version, computing missing steps inline."""
    package = await _ensure_package(db, ecosystem, name, refresh)

    version = await _latest_version_row(db, package)
    if version is None:
        raise FactsUnavailable("upstream_error", f"latest version '{package.latest_version}' has no catalog row")

    await _ensure_probe(db, version)
    await _ensure_classification(db, version, package)

    classification = await classification_block(db, package, version)
    return {
        "ecosystem": package.ecosystem.value,
        "name": package.name,
        "namespace": package.namespace,
        "normalized_name": package.normalized_name,
        "latest_version": package.latest_version,
        "synced_at": package.synced_at,
        "deprecated": package.deprecated,
        "deprecation_message": package.deprecation_message,
        "version_status": version.status.value,
        "classification": classification,
        "registries": await registry_presence(db, package),
    }
