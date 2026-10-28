"""Presence probe (IMPL B4, SPEC §6.6) on a real database and Redis with fake adapters. The probe itself runs in a
transaction that is rolled back; the task wrapper test uses the shared database and cleans up after itself."""

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.base import NormalizedPackage, NormalizedVersion, PackageRef, RegistryConfig, SourceProfile
from app.fetcher import pipeline, sources
from app.fetcher.discovery import NegativeCache
from app.models.enums import Ecosystem, PackageEventKind, PackageOrigin, RegistryProtocol, SyncStatus
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_failure import SyncFailure
from app.models.sync_state import SyncState
from app.services import runs
from app.tasks import presence_probe
from app.tasks.presence_probe import ProbeResult, Target, probe_packages

PLUGIN = pipeline.plugin_for("pypi")


class _Adapter:
    def __init__(self, registry_id: str, *, present: bool = True, exists_error: Exception | None = None,
                 fetch_error: Exception | None = None) -> None:
        self.registry_id = registry_id
        self.present, self.exists_error, self.fetch_error = present, exists_error, fetch_error
        self.exists_calls = 0

    async def exists(self, ref: PackageRef) -> bool:
        self.exists_calls += 1
        if self.exists_error:
            raise self.exists_error
        return self.present

    async def fetch_package(self, ref: PackageRef, lookup) -> NormalizedPackage | None:
        if self.fetch_error:
            raise self.fetch_error
        return _snapshot(ref, self.registry_id)


class _Plugin:
    """The PyPI plugin with fake adapters per registry."""

    def __init__(self, adapters: dict[str, _Adapter]) -> None:
        self.adapters = adapters

    def adapter(self, registry: RegistryConfig) -> _Adapter:
        return self.adapters[registry.id]

    def __getattr__(self, name: str):
        return getattr(PLUGIN, name)


def _snapshot(ref: PackageRef, registry_id: str) -> NormalizedPackage:
    return NormalizedPackage(ref=ref, registry_id=registry_id, display_name=ref.name, revision="1",
                             versions=[NormalizedVersion(version="1.0", status="active", dependencies=[])])


def _ref() -> PackageRef:
    return PLUGIN.coordinates.parse(f"b4-probe-{uuid.uuid4().hex[:10]}")


def _probe_test(test: Callable[..., Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """`test(db, home, other, profile, adapters)`: `home` is the source the packages are known in, `other` the new one."""

    async def run() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            # Savepoints: the probe commits and rolls back per package, which must not end the test transaction.
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                rows = [
                    Registry(ecosystem=Ecosystem.pypi, name=f"Test {n}", slug=f"b4-{n}-{uuid.uuid4().hex[:8]}",
                             base_url=f"https://{n}.invalid/", protocol=RegistryProtocol.pypi_warehouse, priority=p)
                    for n, p in (("home", 10), ("other", 20))
                ]
                db.add_all(rows)
                await db.flush()
                home, other = (sources.registry_config(row) for row in rows)
                profile = SourceProfile(ecosystem="pypi", slug="b4", registries=[home, other])
                adapters = {home.id: _Adapter(home.id), other.id: _Adapter(other.id)}
                monkeypatch.setattr(pipeline, "plugin_for", lambda ecosystem: _Plugin(adapters))
                await db.commit()
                await test(db, home, other, profile, adapters)
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(run())


async def _known_package(db: AsyncSession, home: RegistryConfig) -> PackageRef:
    ref = _ref()
    await pipeline.write_package(db, PLUGIN, [_snapshot(ref, home.id)], origin=PackageOrigin.seed)
    await db.commit()
    return ref


async def _probe(db: AsyncSession, profile: SourceProfile, adapters: dict[str, _Adapter], targets: list[Target],
                 run_id=None) -> ProbeResult:
    async with NegativeCache.open() as cache:
        return await probe_packages(db, _Plugin(adapters), profile, targets, cache, run_id)  # type: ignore[arg-type]


async def _cached(registry: RegistryConfig, ref: PackageRef) -> bool:
    client = Redis.from_url(get_settings().redis_url)
    try:
        return bool(await client.exists(NegativeCache.key(registry, PLUGIN.coordinates.to_purl(ref))))
    finally:
        await client.aclose()


async def _registry_ids(db: AsyncSession, ref: PackageRef) -> set[str]:
    package = await pipeline.find_package(db, ref)
    rows = await db.scalars(select(PackageRegistry.registry_id).where(PackageRegistry.package_id == package.id)
                            .execution_options(populate_existing=True))
    return {str(row) for row in rows}


def test_package_found_in_a_new_source_gets_the_link_and_one_source_added(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(db, home, other, profile, adapters) -> None:
        ref = await _known_package(db, home)
        run = await runs.start_run(db, "presence_probe", "pypi")
        assert run is not None

        result = await _probe(db, profile, adapters, [Target(ref, frozenset({home.id}))], run.id)

        assert (result.checked, result.updated, result.failed) == (1, 1, 0)
        assert result.per_registry == {other.slug: {"probed": 1, "found": 1, "errors": 0}}
        assert adapters[home.id].exists_calls == 0                 # already known there: not asked
        assert await _registry_ids(db, ref) == {home.id, other.id}
        package = await pipeline.find_package(db, ref)
        events = (await db.scalars(select(PackageEvent).where(PackageEvent.package_id == package.id,
                                                              PackageEvent.kind == PackageEventKind.source_added))).all()
        assert len(events) == 1 and events[0].version is None and events[0].run_id == run.id
        assert events[0].details == {"registry": other.slug}

    _probe_test(test, monkeypatch)


def test_a_no_is_cached_and_not_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(db, home, other, profile, adapters) -> None:
        ref = await _known_package(db, home)
        adapters[other.id].present = False
        targets = [Target(ref, frozenset({home.id}))]

        first = await _probe(db, profile, adapters, targets)
        assert (first.checked, first.updated) == (1, 0)
        assert first.per_registry == {other.slug: {"probed": 1, "found": 0, "errors": 0}}
        assert await _cached(other, ref)

        second = await _probe(db, profile, adapters, targets)
        assert adapters[other.id].exists_calls == 1
        assert second.per_registry == {}
        assert await _registry_ids(db, ref) == {home.id}

    _probe_test(test, monkeypatch)


def test_http_error_is_counted_on_the_source_and_the_source_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(db, home, other, profile, adapters) -> None:
        refs = [await _known_package(db, home), await _known_package(db, home)]
        adapters[other.id].exists_error = httpx.ConnectTimeout("timed out")

        result = await _probe(db, profile, adapters, [Target(ref, frozenset({home.id})) for ref in refs])

        assert adapters[other.id].exists_calls == 1                # skipped for the rest of the run
        assert result.per_registry == {other.slug: {"probed": 1, "found": 0, "errors": 1}}
        assert (result.updated, result.failed) == (0, 0)
        assert not await _cached(other, refs[0])                   # an error is not an answer
        state = await db.scalar(select(SyncState).where(SyncState.registry_id == other.id)
                                .execution_options(populate_existing=True))
        assert state.consecutive_failures == 1

    _probe_test(test, monkeypatch)


def test_degraded_sources_are_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(db, home, other, profile, adapters) -> None:
        ref = await _known_package(db, home)
        db.add(SyncState(registry_id=other.id, status=SyncStatus.idle, cursor_data={}, degraded_until=datetime(2999, 1, 1, tzinfo=UTC)))
        await db.commit()

        result = await _probe(db, profile, adapters, [Target(ref, frozenset({home.id}))])

        assert adapters[other.id].exists_calls == 0 and result.checked == 0

    _probe_test(test, monkeypatch)


def test_a_package_that_cannot_be_read_is_recorded_per_purl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(db, home, other, profile, adapters) -> None:
        ref = await _known_package(db, home)
        other_ref = await _known_package(db, home)
        adapters[other.id].fetch_error = httpx.ReadTimeout("slow")
        purl = PLUGIN.coordinates.to_purl(ref)

        result = await _probe(db, profile, adapters, [Target(ref, frozenset({home.id}))])

        assert (result.updated, result.failed) == (0, 1)
        failure = await db.scalar(select(SyncFailure).where(SyncFailure.package_ref == purl))
        assert (failure.operation, failure.ecosystem, failure.package_name, failure.retryable) == (
            "presence_probe", Ecosystem.pypi, ref.name, True)
        assert await _registry_ids(db, ref) == {home.id}

        # The package recovers on the next probe and the record goes away.
        adapters[other.id].fetch_error = None
        result = await _probe(db, profile, adapters, [Target(ref, frozenset({home.id})), Target(other_ref, frozenset({home.id}))])
        assert result.updated == 2
        assert await db.scalar(select(SyncFailure).where(SyncFailure.package_ref == purl)) is None

    _probe_test(test, monkeypatch)


# --- the task: runs journal, overlap guard ------------------------------------------------------------


def _run_db(work: Callable[[AsyncSession], Awaitable]):
    async def go():
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                result = await work(db)
                await db.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def started_at():
    """Runs created by a test are removed afterwards (the shared database keeps the rest)."""
    since = datetime.now(UTC)
    yield since
    _run_db(lambda db: db.execute(delete(Run).where(Run.operation == "presence_probe", Run.started_at >= since)))


def test_the_task_records_a_run_with_per_source_counters(monkeypatch: pytest.MonkeyPatch, started_at: datetime) -> None:
    async def fake_probe(db, plugin, profile, targets, cache, run_id) -> ProbeResult:
        return ProbeResult(checked=3, updated=2, failed=1, per_registry={"x": {"probed": 3, "found": 2, "errors": 0}})

    monkeypatch.setattr(presence_probe, "probe_packages", fake_probe)
    result = asyncio.run(presence_probe._run_presence_probe_async("pypi"))
    assert result["status"] == "completed" and result["packages_updated"] == 2

    async def rows(db: AsyncSession):
        return (await db.scalars(select(Run).where(Run.operation == "presence_probe", Run.started_at >= started_at))).all()

    (run,) = _run_db(rows)
    assert (run.ecosystem, run.status, run.packages_checked, run.packages_updated, run.packages_failed) == (
        Ecosystem.pypi, runs.PARTIAL, 3, 2, 1)
    assert run.packages_total > 0 and run.finished_at is not None
    assert run.per_registry == {"x": {"probed": 3, "found": 2, "errors": 0}}


def test_a_probe_does_not_start_while_another_is_running(monkeypatch: pytest.MonkeyPatch, started_at: datetime) -> None:
    async def fail(*args, **kwargs):
        raise AssertionError("must not probe")

    async def running(db: AsyncSession) -> None:
        await runs.start_run(db, "presence_probe", "pypi")

    _run_db(running)
    monkeypatch.setattr(presence_probe, "probe_packages", fail)
    assert asyncio.run(presence_probe._run_presence_probe_async("pypi"))["status"] == "skipped"


def test_a_crashing_probe_closes_its_run_as_failed(monkeypatch: pytest.MonkeyPatch, started_at: datetime) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("redis is gone")

    monkeypatch.setattr(presence_probe, "probe_packages", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(presence_probe._run_presence_probe_async("pypi"))

    async def rows(db: AsyncSession):
        return (await db.scalars(select(Run).where(Run.operation == "presence_probe", Run.started_at >= started_at))).all()

    (run,) = _run_db(rows)
    assert run.status == runs.FAILED and "redis is gone" in run.error and run.finished_at is not None


def test_a_package_in_a_priority_source_is_not_looked_up_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(db, home, other, profile, adapters) -> None:
        # Dependency confusion: a same-named package published on the public source later must not be merged in.
        ref = await _known_package(db, home)
        corp = dataclasses.replace(home, routing={"mode": "priority"})
        priority_profile = dataclasses.replace(profile, registries=[other, corp])

        result = await _probe(db, priority_profile, adapters, [Target(ref, frozenset({home.id}), frozenset({home.id}))])
        assert (result.checked, result.updated) == (0, 0) and adapters[other.id].exists_calls == 0
        assert await _registry_ids(db, ref) == {home.id}

    _probe_test(test, monkeypatch)
