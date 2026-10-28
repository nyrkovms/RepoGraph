"""Discovery in sources (IMPL B2): negative cache, circuit breaker, `priority` routing. Real Postgres and Redis, fake
adapters; every test runs in a transaction that is rolled back."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.base import NormalizedPackage, NormalizedVersion, PackageRef, RegistryConfig
from app.fetcher import discovery, pipeline, sources
from app.fetcher.discovery import NegativeCache, SourceDegraded
from app.models.enums import Ecosystem, RegistryProtocol
from app.models.registry import Registry
from app.models.sync_state import SyncState

PLUGIN = pipeline.plugin_for("pypi")


class _Adapter:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls = 0

    async def fetch_package(self, ref, lookup):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Plugin:
    """The PyPI plugin with fake adapters per registry."""

    def __init__(self, adapters: dict[str, _Adapter]) -> None:
        self.adapters = adapters
        self.coordinates = PLUGIN.coordinates

    def adapter(self, registry: RegistryConfig) -> _Adapter:
        return self.adapters[registry.id]


def _in_rolled_back_transaction(test: Callable[[AsyncSession, RegistryConfig, RegistryConfig], Awaitable[None]],
                                second_routing: dict | None = None) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                rows = [
                    Registry(ecosystem=Ecosystem.pypi, name=f"Test {n}", slug=f"b2-{n}-{uuid.uuid4().hex[:8]}",
                             base_url=f"https://{n}.invalid/", protocol=RegistryProtocol.pypi_warehouse, priority=p)
                    for n, p in (("first", 10), ("second", 20))
                ]
                db.add_all(rows)
                await db.flush()
                await test(db, sources.registry_config(rows[0]), sources.registry_config(rows[1], second_routing))
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(run())


def _ref() -> PackageRef:
    return PLUGIN.coordinates.parse(f"b2-test-{uuid.uuid4().hex[:10]}")


def _snapshot(ref: PackageRef, registry: RegistryConfig) -> NormalizedPackage:
    return NormalizedPackage(ref=ref, registry_id=registry.id, display_name=ref.name, revision="1",
                             versions=[NormalizedVersion(version="1.0", status="active")])


async def _state(db: AsyncSession, registry: RegistryConfig) -> SyncState | None:
    return await db.scalar(select(SyncState).where(SyncState.registry_id == registry.id)
                           .execution_options(populate_existing=True))


async def _cached(registry: RegistryConfig, ref: PackageRef) -> bool:
    client = Redis.from_url(get_settings().redis_url)
    try:
        return bool(await client.exists(NegativeCache.key(registry, PLUGIN.coordinates.to_purl(ref))))
    finally:
        await client.aclose()


def test_absent_answers_are_cached_per_source() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, second: RegistryConfig) -> None:
        ref = _ref()
        absent, present = _Adapter(None), _Adapter(None)
        plugin = _Plugin({first.id: absent, second.id: present})
        present.answer = _snapshot(ref, second)

        fetched = await discovery.discover(db, plugin, ref, [first, second], None)
        assert [s.registry_id for s in fetched.snapshots] == [second.id] and fetched.missing == [first.id]
        assert await _cached(first, ref) and not await _cached(second, ref)

        await discovery.discover(db, plugin, ref, [first, second], None)
        assert (absent.calls, present.calls) == (1, 2)     # the cached "no" is not asked again

        # An explicit request (seed, admin) asks anyway.
        await discovery.discover(db, plugin, ref, [first, second], None, use_negative_cache=False)
        assert absent.calls == 2

    _in_rolled_back_transaction(test)


def test_found_package_clears_a_stale_absent_entry() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, second: RegistryConfig) -> None:
        ref = _ref()
        adapter = _Adapter(None)
        plugin = _Plugin({first.id: adapter, second.id: _Adapter(None)})
        await discovery.discover(db, plugin, ref, [first], None)
        assert await _cached(first, ref)

        adapter.answer = _snapshot(ref, first)            # published since
        fetched = await discovery.discover(db, plugin, ref, [first], None, use_negative_cache=False)
        assert fetched.snapshots and not await _cached(first, ref)

    _in_rolled_back_transaction(test)


def test_source_errors_are_not_cached_and_count_for_the_breaker() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, second: RegistryConfig) -> None:
        ref = _ref()
        plugin = _Plugin({first.id: _Adapter(httpx.ConnectTimeout("timed out")), second.id: _Adapter(None)})
        with pytest.raises(httpx.ConnectTimeout):
            await discovery.discover(db, plugin, ref, [first, second], None)
        assert not await _cached(first, ref)
        state = await _state(db, first)
        assert state.consecutive_failures == 1 and state.degraded_until is None
        assert "ConnectTimeout" in state.last_error

    _in_rolled_back_transaction(test)


def test_breaker_opens_after_failures_in_a_row_and_answers_close_it() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, second: RegistryConfig) -> None:
        ref = _ref()
        failing = _Adapter(httpx.HTTPStatusError("503", request=httpx.Request("GET", "https://x"), response=httpx.Response(503)))
        other = _Adapter(None)
        plugin = _Plugin({first.id: failing, second.id: other})
        threshold = get_settings().source_breaker_failures
        for _ in range(threshold):
            with pytest.raises(httpx.HTTPStatusError):
                await discovery.discover(db, plugin, ref, [first, second], None)
        state = await _state(db, first)
        assert state.consecutive_failures == threshold and state.degraded_until > datetime.now(UTC)
        assert await discovery.degraded_registry_ids(db) >= {first.id}

        # Discovery skips the degraded source and asks the others.
        other.answer = _snapshot(ref, second)
        fetched = await discovery.discover(db, plugin, ref, [first, second], None)
        assert failing.calls == threshold and [s.registry_id for s in fetched.snapshots] == [second.id]

        # A catalog package living there is not rewritten without it.
        with pytest.raises(SourceDegraded):
            await discovery.fetch_known(db, plugin, ref, [first, second], None)
        assert failing.calls == threshold

        # Any answer of the source closes the breaker.
        await pipeline.write_package(db, PLUGIN, [_snapshot(ref, second)], missing_registry_ids=[first.id])
        state = await _state(db, first)
        assert (state.consecutive_failures, state.degraded_until) == (0, None)

    _in_rolled_back_transaction(test)


def test_priority_source_with_the_package_is_its_only_source() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, corp: RegistryConfig) -> None:
        ref = _ref()
        public, internal = _Adapter(None), _Adapter(None)
        plugin = _Plugin({first.id: public, corp.id: internal})
        public.answer, internal.answer = _snapshot(ref, first), _snapshot(ref, corp)

        # Asked first although it comes later in the profile; the public source is not asked at all.
        fetched = await discovery.discover(db, plugin, ref, [first, corp], None)
        assert [s.registry_id for s in fetched.snapshots] == [corp.id] and public.calls == 0

        # Without the package there, the other sources serve it.
        other = _ref()
        internal.answer, public.answer = None, _snapshot(other, first)
        fetched = await discovery.discover(db, plugin, other, [first, corp], None)
        assert [s.registry_id for s in fetched.snapshots] == [first.id] and fetched.missing == [corp.id]

    _in_rolled_back_transaction(test, second_routing={"mode": "priority"})


def test_degraded_priority_source_stops_discovery() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, corp: RegistryConfig) -> None:
        ref = _ref()
        public = _Adapter(_snapshot(ref, first))
        plugin = _Plugin({first.id: public, corp.id: _Adapter(None)})
        await db.execute(insert_state(corp.id, degraded=True))
        with pytest.raises(SourceDegraded):
            await discovery.discover(db, plugin, ref, [first, corp], None)
        assert public.calls == 0    # the public source must not claim the name meanwhile

    _in_rolled_back_transaction(test, second_routing={"mode": "priority"})


def test_an_absent_answer_closes_the_breaker() -> None:
    async def test(db: AsyncSession, first: RegistryConfig, second: RegistryConfig) -> None:
        ref = _ref()
        await db.execute(insert_state(first.id, degraded=False, failures=3))
        await discovery.discover(db, _Plugin({first.id: _Adapter(None), second.id: _Adapter(None)}), ref, [first], None)
        state = await _state(db, first)
        assert (state.consecutive_failures, state.degraded_until) == (0, None)

    _in_rolled_back_transaction(test)


def insert_state(registry_id: str, *, degraded: bool, failures: int = 5):
    from datetime import timedelta

    from sqlalchemy.dialects.postgresql import insert

    from app.models.enums import SyncStatus, SyncType

    until = datetime.now(UTC) + timedelta(minutes=10) if degraded else None
    return insert(SyncState).values(registry_id=registry_id, sync_type=SyncType.incremental, status=SyncStatus.idle,
                                    cursor_data={}, consecutive_failures=failures, degraded_until=until)
