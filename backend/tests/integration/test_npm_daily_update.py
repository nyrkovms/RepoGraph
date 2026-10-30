"""The daily update of npm through the replication feed (IMPL F4, SPEC §8.2) on the stack's database: the npm plugin,
the npmjs starter source of migration 0008, the packument from the replay fixtures and a canned feed."""

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select, update

from app.db import loop_local_session
from app.ecosystems.base import ChangeSet, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.npm import changes
from app.fetcher import pipeline
from app.models.enums import PackageOrigin
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
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

    async def release_dates() -> list[datetime | None]:
        async with loop_local_session() as db:
            package = await pipeline.find_package(db, MS)
            return list((await db.scalars(select(PackageVersion.release_date)
                                          .where(PackageVersion.package_id == package.id))).all())

    dates = asyncio.run(release_dates())
    assert dates and all(dates), "the full packument's `time` dates every version (Q19)"
    slept: list[float] = []

    async def no_wait(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(daily_update.asyncio, "sleep", no_wait)
    run_ids: list[str] = []
    try:
        # First run: no cursor yet, so the feed starts at its current seq and every npm package is checked once,
        # conditionally: the packument still has the stored ETag (304), nothing is re-read.
        asked = _feed(monkeypatch, 1000, [])
        first = asyncio.run(daily_update._run_daily_update_async("npm"))
        run_ids.append(first["run_id"])
        assert first["status"] == "success" and first["changed"] == 0
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
        cursor = asyncio.run(_cursor())
        assert cursor["last_seq"] == 1010 and len(cursor["pending"]) == 1   # the feed has moved past it
        assert second["per_registry"]["npmjs"]["changed"] == 1

        # The run after that checks it again although the feed no longer names it; still stale, it is left for the
        # weekly check.
        _feed(monkeypatch, 1020, [])
        third = asyncio.run(daily_update._run_daily_update_async("npm"))
        run_ids.append(third["run_id"])
        assert third["status"] == "success" and third["changed"] == 1
        assert "pending" not in asyncio.run(_cursor())
    finally:
        asyncio.run(_cleanup(run_ids))


def test_a_recorded_feed_page_moves_the_cursor_and_finds_its_packages(monkeypatch) -> None:
    """The real `_changes` answer (recorded): numeric seqs, ids as package names, `last_seq` past the update_seq.
    The detector reads that same answer: a second live read of the feed would differ at every recording."""
    feed = "https://replicate.npmjs.com/registry/"
    registry = RegistryConfig(id="r", slug="npmjs", ecosystem="npm", protocol="npm-registry",
                              base_url="https://registry.npmjs.org/", priority=0, config={"changes_url": feed},
                              routing=None, auth_type="none", credentials_ref=None)

    async def run() -> tuple[list[dict], ChangeSet, int]:
        async with changes.make_client(30) as client:
            root = (await client.get(feed)).json()
            target = int(root["update_seq"])
            since = target - 20
            page = (await client.get(f"{feed}_changes", params={"since": since, "limit": 100})).json()

        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/_changes") and request.url.params["since"] == str(since):
                return httpx.Response(200, json=page)
            if request.url.path.endswith("/_changes"):
                return httpx.Response(200, json={"results": [], "last_seq": int(request.url.params["since"])})
            return httpx.Response(200, json=root)

        monkeypatch.setattr(changes, "make_client",
                            lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(answer)))
        named = str(page["results"][0]["id"])
        scope, _, local = named[1:].partition("/") if named.startswith("@") else ("", "", named)
        tracked = [TrackedPackage(ref=PackageRef("npm", scope, local), package_id="p1", revision='"e1"', etag=None,
                                  unavailable=False),
                   TrackedPackage(ref=PackageRef("npm", "", "surely-not-in-this-page-x"), package_id="p2",
                                  revision='"e1"', etag=None, unavailable=False)]
        detector = changes.NpmChangesDetector(registry, feed, page_size=100)
        found = await detector.detect(tracked, {"last_seq": since, "reconciled_at": datetime.now(UTC).isoformat()})
        return page["results"], found, target

    results, found, target = asyncio.run(run())
    assert all(isinstance(entry["seq"], int) and entry["id"] for entry in results)
    assert [(p.package_id, rev) for p, rev in found.changed] == [("p1", 'not:"e1"')]
    assert found.cursor["last_seq"] >= target and found.reconcile_all is False
