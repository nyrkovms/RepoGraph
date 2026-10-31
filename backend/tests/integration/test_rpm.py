"""RPM end to end (IMPL K3-K7): the rpm plugin, rpm-worker in its own container on the synthetic repositories of
`rpm_worker/tests/fixture_repos.py` (recorded under `rpm.fixtures.packagehub.test`), the tree, resolve and the daily
update. Needs the `rpm-worker` service of docker-compose.yml running with the same HTTP_REPLAY_DIR."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import package_detail as routes
from app.config import get_settings
from app.db import loop_local_session
from app.ecosystems.base import PackageRef
from app.fetcher import pipeline
from app.models.enums import Ecosystem, PackageOrigin, RegistryProtocol
from app.models.package_event import PackageEvent
from app.models.registry import Registry
from app.models.run import Run
from app.models.source_profile import SourceProfile, SourceProfileRegistry
from app.models.version import PackageVersion
from app.tasks import daily_update

FIXTURES = "https://rpm.fixtures.packagehub.test/"
SOURCES = [
    ("fixture-rocky-9-baseos", FIXTURES + "rocky-9/BaseOS/$basearch/os/",
     {"namespace": "rocky-9", "vendor": "rocky", "arch": ["x86_64", "aarch64"], "releasever": "9"}),
    ("fixture-rocky-8-appstream", FIXTURES + "rocky-8/AppStream/$basearch/os/",
     {"namespace": "rocky-8", "vendor": "rocky", "arch": ["x86_64"], "releasever": "8", "modular": True}),
]
OPENSSL = PackageRef("rpm", "rocky-9", "openssl")
# The repositories exist only as recorded answers (rpm_worker/tests/fixture_repos.py): nothing to read live.
pytestmark = pytest.mark.skipif(not os.environ.get("HTTP_REPLAY_DIR"), reason="RPM fixture repositories replay only")


async def _sources() -> None:
    """The fixture repositories in the default rpm profile (migration 0013 creates it empty)."""
    async with loop_local_session() as db:
        profile = await db.scalar(select(SourceProfile).where(SourceProfile.ecosystem == Ecosystem.rpm,
                                                              SourceProfile.is_default))
        assert profile is not None, "migration 0013 creates the default rpm profile"
        for slug, url, config in SOURCES:
            registry = await db.scalar(select(Registry).where(Registry.slug == slug))
            if registry is None:
                registry = Registry(ecosystem=Ecosystem.rpm, slug=slug, name=slug)
                db.add(registry)
            registry.base_url, registry.protocol, registry.config = url, RegistryProtocol.rpm_repodata, config
            registry.priority, registry.enabled = 10, True
            await db.flush()
            linked = await db.scalar(select(SourceProfileRegistry).where(
                SourceProfileRegistry.profile_id == profile.id, SourceProfileRegistry.registry_id == registry.id))
            if linked is None:
                position = await db.scalar(select(func.coalesce(func.max(SourceProfileRegistry.position), -1) + 1)
                                           .where(SourceProfileRegistry.profile_id == profile.id))
                db.add(SourceProfileRegistry(profile_id=profile.id, registry_id=registry.id, ecosystem=Ecosystem.rpm,
                                             position=position))
        await db.commit()


def test_a_package_is_read_through_rpm_worker_with_superseded_builds_and_followed_daily(monkeypatch) -> None:
    asyncio.run(_sources())

    async def sync() -> dict[str, PackageVersion]:
        async with loop_local_session() as db:
            await pipeline.sync_package(db, OPENSSL, origin=PackageOrigin.manual)
            await db.commit()
            package = await pipeline.find_package(db, OPENSSL)
            rows = (await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id))).all()
            return {row.version: row for row in rows}

    versions = asyncio.run(sync())
    assert {v: r.status.value for v, r in versions.items()} == {"1:3.0.7-26.el9": "superseded", "1:3.0.7-27.el9": "active"}
    assert versions["1:3.0.7-27.el9"].release_date is not None

    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(daily_update.asyncio, "sleep", no_wait)
    result = asyncio.run(daily_update._run_daily_update_async("rpm"))
    assert result["status"] == "success" and result["changed"] == 0, result

    async def cleanup() -> None:
        async with loop_local_session() as db:
            await db.execute(delete(PackageEvent).where(PackageEvent.run_id == uuid.UUID(result["run_id"])))
            await db.execute(delete(Run).where(Run.id == uuid.UUID(result["run_id"])))
            await db.commit()

    asyncio.run(cleanup())


def test_the_tree_finds_the_package_behind_each_capability() -> None:
    asyncio.run(_sources())

    async def check() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                await pipeline.sync_package(db, OPENSSL, origin=PackageOrigin.manual)
                tree = await routes.get_dependencies_tree(
                    "rpm", "rocky-9/openssl", "1:3.0.7-27.el9", db, profile=None, depth=1, include_optional=False,
                    python_version=None, os=None, node_version=None, cpu=None, target_triple=None, features=None,
                    default_features=None, target_framework=None, scopes=None, arch="x86_64")
                found = {(n.name, n.resolved_name, n.resolved_version) for n in tree.dependencies}
                assert ("libssl.so.3()(64bit)", "rocky-9/openssl-libs", "1:3.0.7-27.el9") in found
                assert ("/bin/sh", "rocky-9/bash", "5.1.8-9.el9") in found
                # A versioned capability: libsolv matches it with its relation, the edge leads to that build.
                assert ("openssl-libs(x86-64)", "rocky-9/openssl-libs", "1:3.0.7-27.el9") in found
                assert all(n.dependency_type == "runtime" for n in tree.dependencies)   # recommends on request only
                artifacts = await routes.list_artifacts("rpm", "rocky-9/openssl", "1:3.0.7-27.el9", db)
                assert {(a.arch, a.artifact_type) for a in artifacts.data} == {("x86_64", "rpm"), ("aarch64", "rpm")}
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(check())


def test_resolve_follows_the_default_and_the_asked_module_stream(client) -> None:
    asyncio.run(_sources())
    body = {"ecosystem": "rpm", "roots": [{"purl": "pkg:rpm/rocky/nodejs?distro=rocky-8"}], "options": {"timeout_s": 120}}
    response = client.post("/api/v1/resolve", json=body)
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["error"] is None and graph["strategy"] == "libsolv", graph["error"]
    assert {(n["name"], n["version"]) for n in graph["nodes"]} == {
        ("rocky-8/nodejs", "1:18.20.4-1.module+el8.10.0+1"), ("rocky-8/glibc", "2.28-251.el8")}
    streams = client.post("/api/v1/resolve", json={**body, "environment": {"module_streams": {"nodejs": "20"}}}).json()
    assert ("rocky-8/nodejs", "1:20.16.0-1.module+el8.10.0+2") in {(n["name"], n["version"]) for n in streams["nodes"]}
    disabled = client.post("/api/v1/resolve", json={**body, "environment": {"disabled_modules": ["nodejs"]}}).json()
    assert ("rocky-8/nodejs", "1:10.24.0-1.el8") in {(n["name"], n["version"]) for n in disabled["nodes"]}
