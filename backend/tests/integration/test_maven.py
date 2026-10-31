"""Maven end to end on the stack's database (IMPL J4, J5): the maven plugin, the maven-central starter source of
migration 0012, the maven2 adapter on the replay fixtures, the tree, resolve and the daily update."""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import package_detail as routes
from app.config import get_settings
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.fetcher import pipeline
from app.models.enums import PackageOrigin
from app.models.package_event import PackageEvent
from app.models.run import Run
from app.models.version import PackageVersion
from app.tasks import daily_update, maven_dates

SPRING_CORE = PackageRef("maven", "org.springframework", "spring-core")
OLD = datetime(2001, 1, 1, tzinfo=UTC)


async def _versions(db: AsyncSession, ref: PackageRef) -> dict[str, PackageVersion]:
    package = await pipeline.find_package(db, ref)
    rows = (await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id))).all()
    return {row.version: row for row in rows}


async def _run(ecosystem: str) -> dict:
    result = await daily_update._run_daily_update_async(ecosystem)
    async with loop_local_session() as db:
        await db.execute(delete(PackageEvent).where(PackageEvent.run_id == uuid.UUID(result["run_id"])))
        await db.execute(delete(Run).where(Run.id == uuid.UUID(result["run_id"])))
        await db.commit()
    return result


def test_an_artifact_is_read_from_maven_central_and_followed_by_its_metadata_etag(monkeypatch) -> None:
    async def sync() -> tuple[object, dict[str, PackageVersion]]:
        async with loop_local_session() as db:
            await pipeline.sync_package(db, SPRING_CORE, origin=PackageOrigin.manual)
            await db.commit()
            return await pipeline.find_package(db, SPRING_CORE), await _versions(db, SPRING_CORE)

    package, versions = asyncio.run(sync())
    assert package.name == "org.springframework:spring-core" and package.license_spdx == "Apache-2.0"
    assert len(versions) > 300 and not any(v.endswith("-SNAPSHOT") for v in versions)
    by_key = sorted(versions.values(), key=lambda v: v.sort_key, reverse=True)
    latest = next(v for v in by_key if v.raw_metadata.get("packaging"))
    assert latest.raw_metadata["packaging"] == "jar" and latest.release_date is not None   # from the POM read anyway

    # The others are dated in the background, newest first; a sync that does not date them keeps those dates.
    async def stamp(release_date: datetime | None, *ids: object) -> None:
        async with loop_local_session() as db:
            await db.execute(update(PackageVersion).where(PackageVersion.id.in_(ids)).values(release_date=release_date))
            await db.commit()

    # As after a first sync (the database may keep dates from an earlier run of this test).
    asyncio.run(stamp(None, *(v.id for v in versions.values() if v.id != latest.id)))
    counts = asyncio.run(maven_dates._run(limit=5))
    assert counts["dated"] == 5 and counts["failed"] == 0, counts
    asyncio.run(stamp(OLD, versions["6.1.14"].id))
    _, again = asyncio.run(sync())
    assert again["6.1.14"].release_date == OLD
    assert sum(1 for v in again.values() if v.release_date is not None) >= 6

    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(daily_update.asyncio, "sleep", no_wait)
    # The metadata answers 304 to its stored ETag: nothing changed.
    first = asyncio.run(_run("maven"))
    assert first["status"] == "success" and first["changed"] == 0, first


def test_the_tree_follows_scopes_and_versions_read_lazily_through_parents_and_boms() -> None:
    async def check() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                coordinates = "com.fasterxml.jackson.core:jackson-databind"
                await pipeline.sync_package(db, PackageRef("maven", "com.fasterxml.jackson.core", "jackson-databind"),
                                            origin=PackageOrigin.manual)
                kwargs = dict(profile=None, depth=1, include_optional=False, python_version=None, os=None,
                              node_version=None, cpu=None, target_triple=None, features=None, default_features=None,
                              target_framework=None)
                # 2.17.2 is not the latest: its POM, jackson-base, jackson-parent and the jackson BOM are read now.
                tree = await routes.get_dependencies_tree("maven", coordinates, "2.17.2", db, scopes=None, **kwargs)
                assert {(n.name, n.resolved_version) for n in tree.dependencies} == {
                    ("com.fasterxml.jackson.core:jackson-annotations", "2.17.2"),
                    ("com.fasterxml.jackson.core:jackson-core", "2.17.2")}
                assert {n.scope for n in tree.dependencies} == {"compile"}
                with_tests = await routes.get_dependencies_tree("maven", coordinates, "2.17.2", db,
                                                                scopes="compile,runtime,test", **kwargs)
                assert len(with_tests.dependencies) > len(tree.dependencies)
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(check())


def test_a_maven_root_resolves_through_the_api(client) -> None:
    # The shared client: the app's engine pool belongs to its event loop.
    response = client.post("/api/v1/resolve", json={
        "ecosystem": "maven", "roots": [{"purl": "pkg:maven/org.springframework/spring-core@6.1.14"}],
        "options": {"timeout_s": 60}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"] is None and body["strategy"] == "maven-nearest-wins", body
    assert {(n["name"], n["version"]) for n in body["nodes"]} == {
        ("org.springframework:spring-core", "6.1.14"), ("org.springframework:spring-jcl", "6.1.14")}
