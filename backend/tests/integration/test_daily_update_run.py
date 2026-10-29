"""The daily update loop (IMPL B4) on the stack's database with a fake change detector and a fake package write:
which packages are re-read, what the run journal says, and how a failing source makes the run partial."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, update

from app.db import loop_local_session
from app.ecosystems.base import ChangeSet
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.tasks import daily_update


class _Detector:
    def __init__(self, pick) -> None:
        self.pick = pick

    async def detect(self, tracked, cursor):
        return self.pick(list(tracked), cursor)


class _Plugin:
    def __init__(self, detector) -> None:
        self.detector = detector

    def adapter(self, registry):
        return self

    def change_detector(self):
        return self.detector


async def _cleanup(run_id: str) -> None:
    async with loop_local_session() as db:
        await db.execute(delete(Run).where(Run.id == uuid.UUID(run_id)))
        await db.commit()


def test_changed_packages_are_reread_and_the_run_is_journaled(monkeypatch) -> None:
    rewritten: list[str] = []

    def pick(tracked, cursor):
        assert len(tracked) >= 2, "seed the stack first (CI seeds flask, django, requests)"
        return ChangeSet(changed=[(tracked[0], None), (tracked[1], None)], missing=[], cursor={**cursor, "probe": 1})

    class _Written:
        id = uuid.uuid4()   # no package_registries rows: nothing to compare, so not stale

    async def fake_sync(db, ref, *, run_id=None, **_):
        rewritten.append(ref.name)
        return _Written()

    monkeypatch.setattr(daily_update, "plugin_for", lambda ecosystem: _Plugin(_Detector(pick)))
    monkeypatch.setattr(daily_update, "sync_package", fake_sync)

    summary = asyncio.run(daily_update._run_daily_update_async("pypi"))
    try:
        assert summary["status"] == "success" and summary["changed"] == 2 and summary["updated"] == 2
        assert len(rewritten) == 2 and summary["per_registry"]["pypi"]["status"] == "ok"

        async def check() -> None:
            async with loop_local_session() as db:
                run = await db.get(Run, uuid.UUID(summary["run_id"]))
                assert (run.operation, run.ecosystem.value, run.status) == ("daily_update", "pypi", "success")
                assert run.packages_changed == 2 and run.per_registry["pypi"]["changed"] == 2
                assert run.packages_updated == 2

        asyncio.run(check())
    finally:
        asyncio.run(_cleanup(summary["run_id"]))


def test_failing_source_makes_the_run_partial(monkeypatch) -> None:
    def pick(tracked, cursor):
        raise httpx.ConnectError("index down")

    monkeypatch.setattr(daily_update, "plugin_for", lambda ecosystem: _Plugin(_Detector(pick)))
    summary = asyncio.run(daily_update._run_daily_update_async("pypi"))
    try:
        assert summary["status"] == "partial" and summary["changed"] == 0
        assert summary["per_registry"]["pypi"]["status"] == "failed"
        assert "ConnectError" in summary["per_registry"]["pypi"]["error"]
    finally:
        asyncio.run(_cleanup(summary["run_id"]))
        asyncio.run(_close_breaker())


async def _close_breaker() -> None:
    # The failure above counts on the stack's pypi source; other tests must not find it degraded.
    async with loop_local_session() as db:
        await db.execute(update(SyncState).where(SyncState.registry_id == select(Registry.id).where(Registry.slug == "pypi")
                                                 .scalar_subquery()).values(consecutive_failures=0, degraded_until=None))
        await db.commit()


def test_overlapping_run_is_skipped(monkeypatch) -> None:
    async def start() -> str:
        async with loop_local_session() as db:
            run = await daily_update.runs.start_run(db, daily_update.OPERATION, "pypi")
            await db.commit()
            return str(run.id)

    run_id = asyncio.run(start())
    try:
        summary = asyncio.run(daily_update._run_daily_update_async("pypi"))
        assert summary["status"] == "skipped" and summary["running_run_id"] == run_id
    finally:
        asyncio.run(_cleanup(run_id))


def test_outcomes_of_the_package_write(monkeypatch) -> None:
    """Gone everywhere -> unavailable; a source error -> a failure keyed by purl."""
    from app.connectors.base import PackageNotFound
    from app.models.package import Package
    from app.models.sync_failure import SyncFailure

    picked: list = []

    def pick(tracked, cursor):
        picked.extend(tracked[:2])
        return ChangeSet(changed=[(tracked[0], None)], missing=[tracked[1]], cursor=cursor)

    async def fake_sync(db, ref, *, run_id=None, **_):
        if ref.name == picked[1].ref.name:
            raise PackageNotFound(ref.name)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(daily_update, "plugin_for", lambda ecosystem: _Plugin(_Detector(pick)))
    monkeypatch.setattr(daily_update, "sync_package", fake_sync)
    summary = asyncio.run(daily_update._run_daily_update_async("pypi"))

    async def check_and_restore() -> None:
        async with loop_local_session() as db:
            failure = await db.scalar(select(SyncFailure).where(SyncFailure.operation == "daily_update",
                                                                SyncFailure.package_ref == f"pkg:pypi/{picked[0].ref.name}"))
            gone = await db.get(Package, uuid.UUID(picked[1].package_id))
            assert failure is not None and failure.ecosystem.value == "pypi"
            assert gone.unavailable_at is not None
            # Restore the shared catalog for the other tests.
            gone.unavailable_at = None
            await db.execute(update(PackageRegistry).where(PackageRegistry.package_id == gone.id).values(missing_since=None))
            await db.execute(delete(SyncFailure).where(SyncFailure.id == failure.id))
            await db.execute(delete(PackageEvent).where(PackageEvent.run_id == uuid.UUID(summary["run_id"])))
            await db.commit()

    try:
        assert (summary["status"], summary["failed"], summary["unavailable"]) == ("partial", 1, 1)
        asyncio.run(check_and_restore())
    finally:
        asyncio.run(_cleanup(summary["run_id"]))


def test_degraded_source_is_skipped_by_change_detection(monkeypatch) -> None:
    def pick(tracked, cursor):
        raise AssertionError("a degraded source must not be asked")

    async def degrade(on: bool) -> None:
        async with loop_local_session() as db:
            pypi = select(Registry.id).where(Registry.slug == "pypi").scalar_subquery()
            until = datetime.now(UTC) + timedelta(minutes=10) if on else None
            await db.execute(update(SyncState).where(SyncState.registry_id == pypi).values(degraded_until=until))
            await db.commit()

    monkeypatch.setattr(daily_update, "plugin_for", lambda ecosystem: _Plugin(_Detector(pick)))
    asyncio.run(degrade(True))
    try:
        summary = asyncio.run(daily_update._run_daily_update_async("pypi"))
        assert summary["status"] == "partial" and summary["per_registry"]["pypi"] == {"status": "degraded"}
        asyncio.run(_cleanup(summary["run_id"]))
    finally:
        asyncio.run(degrade(False))
