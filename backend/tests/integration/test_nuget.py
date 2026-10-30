"""NuGet end to end on the stack's database (IMPL I3, I4, I6): the nuget plugin, the nuget-org starter source of
migration 0011, registrations from the replay fixtures and a canned catalog."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import package_detail as routes
from app.config import get_settings
from app.db import loop_local_session
from app.ecosystems.base import NormalizedDependency, NormalizedPackage, NormalizedVersion, PackageRef
from app.ecosystems.nuget import changes
from app.fetcher import pipeline
from app.models.enums import PackageOrigin
from app.models.package_event import PackageEvent
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.tasks import daily_update

NEWTONSOFT = PackageRef("nuget", "", "newtonsoft.json")
CATALOG = "https://api.nuget.org/v3/catalog0/index.json"


def _catalog(monkeypatch, entries: list[dict]) -> list[str]:
    asked: list[str] = []
    stamp = max((e["commitTimeStamp"] for e in entries), default="2000-01-01T00:00:00Z")

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if str(request.url) == CATALOG:
            return httpx.Response(200, json={"items": [{"@id": "https://catalog.example/page1.json",
                                                        "commitTimeStamp": stamp}]})
        return httpx.Response(200, json={"items": entries})

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


def test_a_package_is_read_from_its_registration_then_followed_through_the_catalog(monkeypatch) -> None:
    async def sync() -> tuple[object, list[PackageVersion]]:
        async with loop_local_session() as db:
            await pipeline.sync_package(db, NEWTONSOFT, origin=PackageOrigin.manual)
            source = select(Registry.id).where(Registry.slug == "nuget-org").scalar_subquery()
            await db.execute(update(SyncState).where(SyncState.registry_id == source).values(cursor_data={}))
            await db.commit()
            package = await pipeline.find_package(db, NEWTONSOFT)
            versions = list((await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id)))
                            .all())
            return package, versions

    package, versions = asyncio.run(sync())
    assert package.name == "Newtonsoft.Json" and package.license_spdx == "MIT" and len(versions) > 50
    assert all(v.release_date for v in versions if v.status.value == "active")

    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(daily_update.asyncio, "sleep", no_wait)
    asked = _catalog(monkeypatch, [])
    first = asyncio.run(_run("nuget"))
    assert first["status"] == "success" and first["changed"] == 0 and asked == []

    since = datetime.now(UTC) - timedelta(minutes=1)

    async def rewind() -> None:
        async with loop_local_session() as db:
            source = select(Registry.id).where(Registry.slug == "nuget-org").scalar_subquery()
            state = await db.scalar(select(SyncState).where(SyncState.registry_id == source))
            state.cursor_data = {**state.cursor_data, "commit": since.isoformat()}
            await db.commit()

    asyncio.run(rewind())
    stamp = (since + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    asked = _catalog(monkeypatch, [{"@type": "nuget:PackageDetails", "nuget:id": "Newtonsoft.Json",
                                    "nuget:version": "99.0.0", "commitTimeStamp": stamp}])
    second = asyncio.run(_run("nuget"))
    assert asked and second["status"] == "success"
    # The replayed registration still has the stored ETag: a lagging CDN, retried once and left for the next run.
    assert second["changed"] == 1 and second["per_registry"]["nuget-org"]["changed"] == 1


def test_the_tree_takes_the_group_of_the_target_framework() -> None:
    nuget = pipeline.plugin_for("nuget")

    async def check() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                source = await db.scalar(select(Registry).where(Registry.slug == "nuget-org"))
                suffix = uuid.uuid4().hex[:8]
                root, std, net10 = f"root{suffix}", f"std{suffix}", f"net10{suffix}"
                groups = [".NETStandard2.0", "net10.0"]

                def dep(name: str, group: str) -> NormalizedDependency:
                    return NormalizedDependency(target=PackageRef("nuget", "", name), constraint="[1.0.0, )",
                                                dependency_type="runtime", raw_spec=f"{name} [1.0.0, )",
                                                environment_markers={"dotnet_target_framework": group},
                                                details={"groups": groups})

                for name, deps in ((std, []), (net10, []), (root, [dep(std, ".NETStandard2.0"), dep(net10, "net10.0")])):
                    snapshot = NormalizedPackage(
                        ref=PackageRef("nuget", "", name), registry_id=str(source.id), display_name=name, revision="1",
                        versions=[NormalizedVersion(version="1.0.0", status="active", dependencies=deps)])
                    await pipeline.write_package(db, nuget, [snapshot], missing_registry_ids=(), origin=PackageOrigin.seed)
                await db.flush()

                async def tree(framework: str | None) -> list[str]:
                    response = await routes.get_dependencies_tree(
                        "nuget", root, "1.0.0", db, profile=None, depth=1, include_optional=False, python_version=None,
                        os=None, node_version=None, cpu=None, target_triple=None, features=None, default_features=None,
                        target_framework=framework)
                    return [node.name for node in response.dependencies]

                assert await tree(None) == [std]                 # net8.0 by default: the netstandard group
                assert await tree("net10.0") == [net10]
                try:
                    await tree("net472")
                except routes.HTTPException as exc:
                    assert exc.status_code == 422
                else:
                    raise AssertionError("an unsupported framework is a 422")
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(check())
