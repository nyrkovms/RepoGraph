"""The daily update of npm through the replication feed (IMPL F4, SPEC §8.2) on the stack's database: the npm plugin,
the npmjs starter source of migration 0008, the packument from the replay fixtures and a canned feed."""

import asyncio
import uuid

import httpx
from sqlalchemy import delete, select, update

from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.ecosystems.npm import changes
from app.fetcher import pipeline
from app.models.enums import PackageOrigin
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.tasks import daily_update

MS = PackageRef("npm", "", "ms")


def _feed(monkeypatch, update_seq: int, changed: list[str]) -> list[str]:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path.endswith("/_changes"):
            results = [{"seq": update_seq, "id": name, "changes": [{"rev": "9-z"}]} for name in changed]
            return httpx.Response(200, json={"results": results, "last_seq": update_seq})
        return httpx.Response(200, json={"db_name": "registry", "update_seq": update_seq})

    monkeypatch.setattr(changes, "make_client",
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


async def _cursor() -> dict:
    async with loop_local_session() as db:
        return await db.scalar(select(SyncState.cursor_data).join(Registry, Registry.id == SyncState.registry_id)
                               .where(Registry.slug == "npmjs")) or {}


async def _cleanup(run_ids: list[str]) -> None:
    async with loop_local_session() as db:
        await db.execute(delete(Run).where(Run.id.in_([uuid.UUID(r) for r in run_ids])))
        await db.commit()


def test_npm_packages_are_rechecked_through_the_feed(monkeypatch) -> None:
    async def seed() -> None:
        async with loop_local_session() as db:
            await pipeline.sync_package(db, MS, origin=PackageOrigin.manual)
            # The feed has not been read on this database yet (also when the test runs again).
            npmjs = select(Registry.id).where(Registry.slug == "npmjs").scalar_subquery()
            await db.execute(update(SyncState).where(SyncState.registry_id == npmjs).values(cursor_data={}))
            await db.commit()

    asyncio.run(seed())
    slept: list[float] = []

    async def no_wait(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(daily_update.asyncio, "sleep", no_wait)
    run_ids: list[str] = []
    try:
        # First run: no cursor yet, so the feed starts at its current seq and every npm package is checked once.
        asked = _feed(monkeypatch, 1000, [])
        first = asyncio.run(daily_update._run_daily_update_async("npm"))
        run_ids.append(first["run_id"])
        assert first["status"] == "success" and first["changed"] >= 1
        assert not any("_changes" in url for url in asked)
        cursor = asyncio.run(_cursor())
        assert cursor["last_seq"] == 1000 and "reconciled_at" in cursor

        # Next run: the feed names `ms`; the replayed packument still has the stored ETag, which reads as a lagging
        # CDN: the package is retried once, then left for the next run.
        _feed(monkeypatch, 1010, ["ms", "not-in-the-catalog"])
        second = asyncio.run(daily_update._run_daily_update_async("npm"))
        run_ids.append(second["run_id"])
        assert second["status"] == "success" and second["changed"] == 1
        assert slept, "a stale answer is retried after daily_update_stale_retry_seconds"
        assert asyncio.run(_cursor())["last_seq"] == 1010
        assert second["per_registry"]["npmjs"]["changed"] == 1
    finally:
        asyncio.run(_cleanup(run_ids))
