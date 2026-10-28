"""Asking sources for a package (SPEC-MULTI-ECOSYSTEM.md §6.6): discovery of new packages with a negative cache,
re-reading known ones, and the per-source circuit breaker kept in sync_states."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ecosystems.base import EcosystemPlugin, NormalizedPackage, PackageRef, RegistryConfig, SourceLookup
from app.ecosystems.routing import is_priority
from app.models.enums import SyncStatus, SyncType
from app.models.sync_state import SyncState

logger = logging.getLogger(__name__)


class SourceDegraded(Exception):
    """A source of a catalog package is behind its circuit breaker; the package is not rewritten without it."""

    def __init__(self, registry: RegistryConfig) -> None:
        super().__init__(f"source {registry.slug} is degraded")
        self.registry = registry


@dataclass(slots=True)
class Fetched:
    snapshots: list[NormalizedPackage] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # registries that answered "no such package"


# --- negative cache --------------------------------------------------------------------------------


class NegativeCache:
    """"The source has no such package" (404), per source, in Redis with the source's TTL. Source errors are never
    stored (§6.6). Redis being unavailable only costs a request to the source."""

    PREFIX = "absent:v1"

    def __init__(self, client: Redis) -> None:
        self.client = client

    @classmethod
    @asynccontextmanager
    async def open(cls) -> AsyncIterator["NegativeCache"]:
        # A client per use: its connections belong to the running loop, and lazy fetch runs in loops of its own.
        client = Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            yield cls(client)
        finally:
            await client.aclose()

    @classmethod
    def key(cls, registry: RegistryConfig, purl: str) -> str:
        return f"{cls.PREFIX}:{registry.id}:{purl}"

    @staticmethod
    def ttl_seconds(registry: RegistryConfig) -> int:
        value = registry.config.get("negative_cache_ttl_seconds")
        return int(value) if isinstance(value, int | str) else get_settings().source_negative_cache_ttl_seconds

    async def is_absent(self, registry: RegistryConfig, purl: str) -> bool:
        try:
            return bool(await self.client.exists(self.key(registry, purl)))
        except Exception:  # noqa: BLE001
            return False

    async def remember_absent(self, registry: RegistryConfig, purl: str) -> None:
        ttl = self.ttl_seconds(registry)
        if ttl <= 0:
            return
        try:
            await self.client.setex(self.key(registry, purl), ttl, "1")
        except Exception:  # noqa: BLE001
            logger.debug("Negative cache write failed for %s in %s", purl, registry.slug)

    async def forget(self, registry: RegistryConfig, purl: str) -> None:
        try:
            await self.client.delete(self.key(registry, purl))
        except Exception:  # noqa: BLE001
            logger.debug("Negative cache delete failed for %s in %s", purl, registry.slug)


# --- circuit breaker -------------------------------------------------------------------------------


async def degraded_registry_ids(db: AsyncSession) -> set[str]:
    rows = await db.scalars(select(SyncState.registry_id).where(SyncState.degraded_until > func.now()))
    return {str(registry_id) for registry_id in rows}


async def record_source_failure(db: AsyncSession, registry: RegistryConfig, error: BaseException) -> None:
    """Count a failed request; after `source_breaker_failures` in a row the source is degraded for the cooldown.

    Written in a transaction of its own: the caller rolls its work back after a source error, the count must stay.
    A lock held by the caller's own transaction would wait forever, hence the short lock timeout.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    degraded_until = now + timedelta(seconds=settings.source_breaker_cooldown_seconds)
    message = f"{registry.slug}: {type(error).__name__}: {error}"[:1000]
    failures = SyncState.consecutive_failures + 1
    stmt = insert(SyncState).values(
        registry_id=registry.id,
        sync_type=SyncType.incremental,
        status=SyncStatus.idle,
        last_error=message,
        cursor_data={},
        consecutive_failures=1,
        degraded_until=degraded_until if settings.source_breaker_failures <= 1 else None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["registry_id"],
        set_={
            "consecutive_failures": failures,
            "last_error": message,
            "degraded_until": case(
                (failures >= settings.source_breaker_failures, degraded_until), else_=SyncState.degraded_until
            ),
        },
    )
    try:
        async with AsyncSession(bind=db.bind) as side:
            await side.execute(text("SET LOCAL lock_timeout = '2s'"))
            await side.execute(stmt)
            await side.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Could not record a failure of source %s", registry.slug, exc_info=True)


async def _fetch(
    db: AsyncSession, plugin: EcosystemPlugin, registry: RegistryConfig, ref: PackageRef, lookup: SourceLookup
) -> NormalizedPackage | None:
    try:
        return await plugin.adapter(registry).fetch_package(ref, lookup)
    except httpx.HTTPError as exc:
        # Only failures of the source count for the breaker; a parsing bug in the adapter is not the source's fault.
        await record_source_failure(db, registry, exc)
        raise


# --- asking sources --------------------------------------------------------------------------------


async def fetch_known(
    db: AsyncSession,
    plugin: EcosystemPlugin,
    ref: PackageRef,
    registries: Sequence[RegistryConfig],
    lookup: SourceLookup,
) -> Fetched:
    """A catalog package: every source where it lives is asked. A degraded one stops the write (SourceDegraded):
    without it, versions only it publishes would look removed."""
    degraded = await degraded_registry_ids(db)
    for registry in registries:
        if registry.id in degraded:
            raise SourceDegraded(registry)

    fetched = Fetched()
    for registry in registries:
        snapshot = await _fetch(db, plugin, registry, ref, lookup)
        if snapshot is None:
            fetched.missing.append(registry.id)
        else:
            fetched.snapshots.append(snapshot)
    return fetched


async def discover(
    db: AsyncSession,
    plugin: EcosystemPlugin,
    ref: PackageRef,
    registries: Sequence[RegistryConfig],
    lookup: SourceLookup,
    *,
    use_negative_cache: bool = True,
) -> Fetched:
    """A package new to the catalog, from the sources routing gave (`sources_for`).

    Degraded sources are skipped with a warning. `priority` sources are asked first: the first one that has the
    package is its only source. Answers "no such package" are cached; `use_negative_cache=False` (an explicit seed
    or admin request) asks anyway. `fetch_package` doubles as the existence probe: for the protocols we have the
    probe would be the same request.
    """
    degraded = await degraded_registry_ids(db)
    asked = []
    for registry in registries:
        if registry.id in degraded:
            logger.warning("Source %s is degraded: skipped in discovery of %s", registry.slug, plugin.coordinates.display(ref))
        else:
            asked.append(registry)

    purl = plugin.coordinates.to_purl(ref)
    fetched = Fetched()
    async with NegativeCache.open() as cache:
        for group, first_wins in (([r for r in asked if is_priority(r)], True), ([r for r in asked if not is_priority(r)], False)):
            for registry in group:
                if use_negative_cache and await cache.is_absent(registry, purl):
                    continue
                snapshot = await _fetch(db, plugin, registry, ref, lookup)
                if snapshot is None:
                    await cache.remember_absent(registry, purl)
                    fetched.missing.append(registry.id)
                    continue
                if not use_negative_cache:
                    await cache.forget(registry, purl)
                if first_wins:
                    return Fetched(snapshots=[snapshot])
                fetched.snapshots.append(snapshot)
    return fetched
