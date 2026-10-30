"""Go end to end on the stack's database (IMPL H3, H4, H6): the go plugin, the proxy-golang-org starter source of
migration 0010, proxy answers from the replay fixtures and a canned module index."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, update

from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.ecosystems.go import changes
from app.fetcher import pipeline
from app.models.enums import PackageOrigin
from app.models.package_event import PackageEvent
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.tasks import daily_update

GIN = PackageRef("go", "", "github.com/gin-gonic/gin")


def _index(monkeypatch, entries: list[dict]) -> list[str]:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text="".join(json.dumps(e) + "\n" for e in entries))

    monkeypatch.setattr(changes, "make_client",
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


async def _run(ecosystem: str) -> dict:
    result = await daily_update._run_daily_update_async(ecosystem)
    async with loop_local_session() as db:
        await db.execute(delete(PackageEvent).where(PackageEvent.run_id == uuid.UUID(result["run_id"])))
        await db.execute(delete(Run).where(Run.id == uuid.UUID(result["run_id"])))
        await db.commit()
    return result


def test_a_module_is_read_from_the_proxy_then_followed_through_the_index(monkeypatch) -> None:
    async def sync() -> tuple[object, list[PackageVersion]]:
        async with loop_local_session() as db:
            await pipeline.sync_package(db, GIN, origin=PackageOrigin.manual)
            # The index has not been read on this database yet (also when the test runs again).
            source = select(Registry.id).where(Registry.slug == "proxy-golang-org").scalar_subquery()
            await db.execute(update(SyncState).where(SyncState.registry_id == source).values(cursor_data={}))
            await db.commit()
            package = await pipeline.find_package(db, GIN)
            versions = list((await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id)))
                            .all())
            return package, versions

    package, versions = asyncio.run(sync())
    assert package.name == "github.com/gin-gonic/gin" and len(versions) > 20
    assert all(v.release_date for v in versions), "every version is dated from its .info"
    latest = max(versions, key=lambda v: v.sort_key)
    assert latest.engines and "go" in latest.engines

    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(daily_update.asyncio, "sleep", no_wait)
    # First run: no cursor yet, so the index starts now and every module is checked once through its list.
    asked = _index(monkeypatch, [])
    first = asyncio.run(_run("go"))
    assert first["status"] == "success" and first["changed"] == 0 and asked == []

    # Next run: the index names gin (and a pseudo-version, which does not count for a module with tags).
    since = datetime.now(UTC) - timedelta(minutes=1)

    async def rewind() -> None:
        async with loop_local_session() as db:
            source = select(Registry.id).where(Registry.slug == "proxy-golang-org").scalar_subquery()
            state = await db.scalar(select(SyncState).where(SyncState.registry_id == source))
            state.cursor_data = {**state.cursor_data, "since": since.isoformat()}
            await db.commit()

    asyncio.run(rewind())
    stamp = (since + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    asked = _index(monkeypatch, [
        {"Path": GIN.name, "Version": "v1.99.0", "Timestamp": stamp},
        {"Path": "golang.org/x/exp", "Version": "v0.0.0-20261029000000-abcdefabcdef", "Timestamp": stamp},
    ])
    second = asyncio.run(_run("go"))
    assert asked and second["status"] == "success"
    # The replayed list still has the stored revision: a lagging proxy, retried once and left for the next run.
    assert second["changed"] == 1 and second["per_registry"]["proxy-golang-org"]["changed"] == 1
