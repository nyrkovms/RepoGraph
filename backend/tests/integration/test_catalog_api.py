"""The catalog routes per ecosystem (IMPL E0-C C1b): listing, search, stats, updates and the admin sync status.

The database is shared and not rolled back between tests: everything created here is prefixed `c1b-` and removed at
the end of the module. Nothing talks to the network; the second ecosystem (npm) exists only as rows.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import admin as admin_api
from app.config import get_settings
from app.models.dependency import Dependency
from app.models.enums import (
    DependencyType,
    Ecosystem,
    PackageEventKind,
    PackageOrigin,
    RegistryProtocol,
    SyncStatus,
)
from app.models.package import Package
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.run import Run
from app.models.sync_state import SyncState
from app.models.version import PackageVersion, VersionSource

PREFIX = "c1b-"
API = "/api/v1"


def run_db(work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                result = await work(db)
                await db.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _registry(slug: str, ecosystem: Ecosystem, protocol: RegistryProtocol, priority: int, enabled: bool = True) -> Registry:
    return Registry(ecosystem=ecosystem, name=slug, slug=slug, base_url="https://x.invalid/", protocol=protocol,
                    priority=priority, enabled=enabled)


def _package(registry: Registry, ecosystem: Ecosystem, name: str, *, namespace: str = "", origin: PackageOrigin,
             latest: str | None, now: datetime) -> Package:
    return Package(id=uuid.uuid4(), ecosystem=ecosystem, namespace=namespace, name=name, normalized_name=name,
                   primary_registry_id=registry.id, latest_version=latest, updated_at=now, synced_at=now,
                   raw_metadata={}, origin=origin)


def _link(package: Package, registry: Registry, now: datetime, missing: bool = False) -> PackageRegistry:
    return PackageRegistry(package_id=package.id, registry_id=registry.id, ecosystem=package.ecosystem,
                           first_seen_at=now, missing_since=now if missing else None)


def _event(package: Package, kind: PackageEventKind, now: datetime, version: str | None = None,
           details: dict | None = None) -> PackageEvent:
    return PackageEvent(package_id=package.id, kind=kind, version=version, detected_at=now, details=details or {})


async def _build(db: AsyncSession, now: datetime) -> dict[str, Any]:
    npm_hi = _registry(f"{PREFIX}npm-hi", Ecosystem.npm, RegistryProtocol.npm_registry, 1)
    npm_lo = _registry(f"{PREFIX}npm-lo", Ecosystem.npm, RegistryProtocol.npm_registry, 200, enabled=False)
    pypi2 = _registry(f"{PREFIX}pypi2", Ecosystem.pypi, RegistryProtocol.pypi_simple, 50)
    db.add_all([npm_hi, npm_lo, pypi2])
    await db.flush()

    widget = _package(npm_hi, Ecosystem.npm, f"{PREFIX}widget", origin=PackageOrigin.seed, latest=None, now=now)
    ui = _package(npm_hi, Ecosystem.npm, f"{PREFIX}ui", namespace="c1b", origin=PackageOrigin.manual, latest="1.0.0",
                  now=now)
    pywidget = _package(pypi2, Ecosystem.pypi, f"{PREFIX}widget", origin=PackageOrigin.dependency, latest="0.1", now=now)
    db.add_all([widget, ui, pywidget])
    await db.flush()
    db.add_all([_link(widget, npm_hi, now), _link(ui, npm_hi, now), _link(widget, npm_lo, now, missing=True),
                _link(pywidget, pypi2, now)])

    version = PackageVersion(id=uuid.uuid4(), package_id=widget.id, version="1.0.0", raw_metadata={},
                             sort_key=b"\x01", is_prerelease=False)
    db.add(version)
    await db.flush()
    db.add(VersionSource(version_id=version.id, registry_id=npm_hi.id, first_seen_at=now))
    for variant in (None, npm_lo.id):
        db.add(Dependency(version_id=version.id, dependency_name="left-pad", dependency_ecosystem=Ecosystem.npm,
                          version_constraint="^1", dependency_type=DependencyType.runtime, raw_spec="left-pad@^1",
                          variant_registry_id=variant))

    db.add_all([
        SyncState(registry_id=npm_hi.id, status=SyncStatus.idle, last_success_at=now + timedelta(days=1),
                  cursor_data={"seq": 5}),
        SyncState(registry_id=npm_lo.id, status=SyncStatus.failed, last_error="x", cursor_data={"a": 1},
                  consecutive_failures=3, degraded_until=now + timedelta(hours=1)),
    ])
    db.add_all([
        _event(widget, PackageEventKind.release_added, now, "1.0.0", {"previous_version": None, "bump": "major"}),
        _event(widget, PackageEventKind.release_deprecated, now, "0.9.0", {"message": "old"}),
        _event(widget, PackageEventKind.source_added, now, None, {"registry": npm_lo.slug}),
        _event(ui, PackageEventKind.release_added, now, "1.0.0", {"previous_version": None, "bump": "minor"}),
        _event(pywidget, PackageEventKind.release_yanked, now, "0.1"),
    ])
    return {"npm_hi": npm_hi.slug, "npm_lo": npm_lo.slug, "pypi2": pypi2.slug}


async def _cleanup(db: AsyncSession) -> None:
    registry_ids = (await db.scalars(select(Registry.id).where(Registry.slug.like(f"{PREFIX}%")))).all()
    package_ids = (await db.scalars(select(Package.id).where(Package.name.like(f"{PREFIX}%")))).all()
    version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(package_ids)))).all()
    await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
    await db.execute(delete(VersionSource).where(VersionSource.version_id.in_(version_ids)))
    await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
    await db.execute(delete(PackageEvent).where(PackageEvent.package_id.in_(package_ids)))
    await db.execute(delete(PackageRegistry).where(PackageRegistry.package_id.in_(package_ids)))
    await db.execute(delete(Package).where(Package.id.in_(package_ids)))
    await db.execute(delete(SyncState).where(SyncState.registry_id.in_(registry_ids)))
    await db.execute(delete(Registry).where(Registry.id.in_(registry_ids)))
    await db.execute(delete(Run).where(Run.operation.like(f"{PREFIX}%")))
    await db.execute(delete(Run).where(Run.started_at > datetime.now(UTC) + timedelta(hours=100)))


@pytest.fixture(scope="module")
def world():
    now = datetime.now(UTC)
    run_db(_cleanup)
    slugs = run_db(lambda db: _build(db, now))
    yield SimpleNamespace(now=now, **slugs)
    run_db(_cleanup)


@pytest.fixture
def auth(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


def names(payload: dict) -> list[tuple[str, str]]:
    return [(row["ecosystem"], row["purl"]) for row in payload["data"] if row["name"].startswith(PREFIX)]


# --------------------------------------------------------------------------- /packages and /search


def test_packages_filtered_by_ecosystem_and_registry(client: TestClient, world) -> None:
    payload = client.get(f"{API}/packages", params={"ecosystem": "npm", "registry": world.npm_hi}).json()
    assert [(row["normalized_name"], row["namespace"], row["purl"], row["origin"], row["latest_version"])
            for row in payload["data"]] == [
        (f"{PREFIX}ui", "c1b", f"pkg:npm/%40c1b/{PREFIX}ui", "manual", "1.0.0"),   # scope stored without @
        (f"{PREFIX}widget", "", f"pkg:npm/{PREFIX}widget", "seed", None),
    ]
    assert all(row["ecosystem"] == "npm" for row in payload["data"])


def test_packages_registry_alone_and_unknown_registry(client: TestClient, world) -> None:
    assert names(client.get(f"{API}/packages", params={"registry": world.pypi2}).json()) == [
        ("pypi", f"pkg:pypi/{PREFIX}widget")]

    response = client.get(f"{API}/packages", params={"registry": "c1b-no-such-registry"})
    assert response.status_code == 200
    assert response.json() == {"data": [], "cursor": None, "has_more": False}


def test_packages_filtered_by_origin(client: TestClient, world) -> None:
    both = client.get(f"{API}/packages", params={"registry": world.npm_hi, "origin": ["seed", "manual"]}).json()
    assert len(both["data"]) == 2
    seed = client.get(f"{API}/packages", params={"registry": world.npm_hi, "origin": "seed"}).json()
    assert [row["normalized_name"] for row in seed["data"]] == [f"{PREFIX}widget"]
    assert client.get(f"{API}/packages", params={"origin": "bogus"}).status_code == 422
    assert client.get(f"{API}/packages", params={"ecosystem": "cobol"}).status_code == 422


def test_search_lists_every_ecosystem_unless_filtered(client: TestClient, world) -> None:
    everything = names(client.get(f"{API}/search", params={"q": PREFIX, "limit": 50}).json())
    assert sorted(everything) == sorted([
        ("npm", f"pkg:npm/{PREFIX}widget"), ("npm", f"pkg:npm/%40c1b/{PREFIX}ui"), ("pypi", f"pkg:pypi/{PREFIX}widget")])

    pypi = client.get(f"{API}/search", params={"q": PREFIX, "ecosystem": "pypi", "limit": 50}).json()
    assert names(pypi) == [("pypi", f"pkg:pypi/{PREFIX}widget")]

    both = client.get(f"{API}/search", params={"q": PREFIX, "ecosystem": ["npm", "pypi"], "limit": 50}).json()
    assert len(names(both)) == 3

    npm_seed = client.get(f"{API}/search", params={"q": PREFIX, "ecosystem": "npm", "origin": "seed"}).json()
    assert names(npm_seed) == [("npm", f"pkg:npm/{PREFIX}widget")]

    unknown = client.get(f"{API}/search", params={"q": PREFIX, "registry": "c1b-no-such-registry"})
    assert unknown.status_code == 200 and unknown.json()["data"] == []


@pytest.mark.parametrize("sort", ["name", "updated_at", "downloads_total"])
def test_cursor_walks_all_ecosystems(client: TestClient, world, sort: str) -> None:
    seen: list[tuple[str, str]] = []
    cursor = None
    for _ in range(10):
        params = {"q": PREFIX, "limit": 1, "sort": sort} | ({"cursor": cursor} if cursor else {})
        page = client.get(f"{API}/search", params=params).json()
        seen += names(page)
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 3 and len(set(seen)) == 3


# --------------------------------------------------------------------------- /stats


def _db_pypi_versions() -> int:
    async def count(db: AsyncSession) -> int:
        return await db.scalar(select(func.count(PackageVersion.id)).join(Package, Package.id == PackageVersion.package_id)
                               .where(Package.ecosystem == Ecosystem.pypi)) or 0

    return run_db(count)


def test_stats_per_ecosystem(client: TestClient, world) -> None:
    payload = client.get(f"{API}/stats").json()
    by_name = {item["ecosystem"]: item for item in payload["ecosystems"]}
    assert [item["ecosystem"] for item in payload["ecosystems"]] == sorted(by_name)
    assert {"npm", "pypi"} <= by_name.keys()

    # The top-level keys are PyPI's: versions no longer include other ecosystems, the sync time is PyPI's.
    pypi = by_name["pypi"]
    assert payload["ecosystem"] == "pypi"
    assert (payload["packages"], payload["versions"], payload["dependencies"], payload["last_sync_at"]) == (
        pypi["packages"], pypi["versions"], pypi["dependencies"], pypi["last_sync_at"])
    assert payload["versions"] == _db_pypi_versions()
    assert payload["last_sync_at"] != by_name["npm"]["last_sync_at"]

    npm = by_name["npm"]
    assert npm["packages"] >= 2 and npm["versions"] >= 1 and npm["dependencies"] >= 1
    assert npm["last_sync_at"] == (world.now + timedelta(days=1)).isoformat()
    registries = [r for r in npm["registries"] if r["slug"].startswith(PREFIX)]
    assert registries == [
        {"slug": world.npm_hi, "enabled": True, "packages": 2, "versions": 1,
         "last_success_at": (world.now + timedelta(days=1)).isoformat(), "degraded": False},
        {"slug": world.npm_lo, "enabled": False, "packages": 0, "versions": 0,    # its only link is missing
         "last_success_at": None, "degraded": True},
    ]
    assert [r["slug"] for r in by_name["pypi"]["registries"] if r["slug"].startswith(PREFIX)] == [world.pypi2]


def test_stats_dependencies_skip_variants(client: TestClient, world) -> None:
    async def canonical(db: AsyncSession) -> int:
        return await db.scalar(
            select(func.count(Dependency.id)).join(PackageVersion, PackageVersion.id == Dependency.version_id)
            .join(Package, Package.id == PackageVersion.package_id)
            .where(Package.ecosystem == Ecosystem.npm, Dependency.variant_registry_id.is_(None))) or 0

    npm = {item["ecosystem"]: item for item in client.get(f"{API}/stats").json()["ecosystems"]}["npm"]
    assert npm["dependencies"] == run_db(canonical)


# --------------------------------------------------------------------------- /updates


def window(world) -> dict[str, str]:
    return {"since": (world.now - timedelta(hours=1)).isoformat(), "until": (world.now + timedelta(hours=1)).isoformat()}


def test_updates_filters(client: TestClient, world) -> None:
    params = window(world) | {"limit": 200}
    npm = client.get(f"{API}/updates", params=params | {"registry": world.npm_hi}).json()["data"]
    assert {(e["ecosystem"], e["package"], e["purl"]) for e in npm} == {
        ("npm", f"{PREFIX}widget", f"pkg:npm/{PREFIX}widget"), ("npm", f"{PREFIX}ui", f"pkg:npm/%40c1b/{PREFIX}ui")}
    assert len(npm) == 4

    pypi = client.get(f"{API}/updates", params=params | {"ecosystem": "pypi", "registry": world.pypi2}).json()["data"]
    assert [(e["ecosystem"], e["kind"], e["purl"]) for e in pypi] == [
        ("pypi", "release_yanked", f"pkg:pypi/{PREFIX}widget")]

    # The registry and the ecosystem narrow each other down.
    assert client.get(f"{API}/updates", params=params | {"ecosystem": "pypi", "registry": world.npm_hi}).json()["data"] == []
    assert client.get(f"{API}/updates", params=params | {"registry": "c1b-no-such-registry"}).json()["data"] == []


def test_updates_package_accepts_a_purl_or_a_bare_name(client: TestClient, world) -> None:
    params = window(world) | {"registry": world.pypi2}
    by_purl = client.get(f"{API}/updates", params=params | {"package": f"pkg:pypi/{PREFIX.upper()}Widget"}).json()["data"]
    by_name = client.get(f"{API}/updates", params=params | {"package": f"{PREFIX.upper()}Widget"}).json()["data"]
    assert len(by_purl) == 1 and by_purl == by_name
    assert by_purl[0]["ecosystem"] == "pypi"

    # A bare name means PyPI: the npm package of the same name does not match it.
    npm_events = client.get(f"{API}/updates", params=window(world) | {"package": f"{PREFIX}widget", "limit": 200}).json()
    assert {e["ecosystem"] for e in npm_events["data"] if e["package"] == f"{PREFIX}widget"} == {"pypi"}

    for bad in ("pkg:pypi/", "pkg:cobol/x", "pkg:"):
        assert client.get(f"{API}/updates", params=params | {"package": bad}).status_code == 422


def test_updates_summary_groups_by_ecosystem(client: TestClient, world) -> None:
    payload = client.get(f"{API}/updates/summary", params=window(world)).json()
    widgets = [p for p in payload["packages"] if p["name"] == f"{PREFIX}widget"]
    assert sorted((p["ecosystem"], p["purl"]) for p in widgets) == [
        ("npm", f"pkg:npm/{PREFIX}widget"), ("pypi", f"pkg:pypi/{PREFIX}widget")]
    npm_widget = next(p for p in widgets if p["ecosystem"] == "npm")
    assert npm_widget["deprecated"] == ["0.9.0"] and npm_widget["sources_added"] == [world.npm_lo]
    assert npm_widget["flags"] == ["deprecated", "major"]
    assert next(p for p in widgets if p["ecosystem"] == "pypi")["yanked"] == ["0.1"]


def test_updates_summary_filters_and_totals(client: TestClient, world) -> None:
    npm = client.get(f"{API}/updates/summary", params=window(world) | {"registry": world.npm_hi}).json()
    assert sorted(p["purl"] for p in npm["packages"]) == [f"pkg:npm/%40c1b/{PREFIX}ui", f"pkg:npm/{PREFIX}widget"]
    assert npm["totals"] == {"packages": 2, "releases": 2, "yanked": 0, "removed": 0, "deprecated": 1,
                             "sources_added": 1, "vulnerabilities": 0, "unavailable": 0}

    pypi = client.get(f"{API}/updates/summary", params=window(world) | {"ecosystem": "pypi", "registry": world.pypi2}).json()
    assert [p["purl"] for p in pypi["packages"]] == [f"pkg:pypi/{PREFIX}widget"]
    assert pypi["totals"]["yanked"] == 1 and pypi["totals"]["deprecated"] == 0

    empty = client.get(f"{API}/updates/summary", params=window(world) | {"registry": "c1b-no-such-registry"}).json()
    assert empty["packages"] == [] and empty["totals"]["packages"] == 0


async def _add_runs(db: AsyncSession, *runs: Run) -> None:
    db.add_all(runs)


def _daily(ecosystem: Ecosystem, hours: int, status: str, checked: int) -> Run:
    start = datetime.now(UTC) + timedelta(hours=hours)
    return Run(operation="daily_update", ecosystem=ecosystem, status=status, started_at=start,
               finished_at=start + timedelta(minutes=1), packages_checked=checked, packages_changed=2, events_created=3)


def test_updates_summary_last_runs(client: TestClient, world) -> None:
    # Far in the future so that these are the latest whatever else the journal holds.
    run_db(lambda db: _add_runs(
        db, _daily(Ecosystem.pypi, 101, "failed", 1), _daily(Ecosystem.pypi, 102, "partial", 77),
        _daily(Ecosystem.npm, 101, "success", 5)))
    try:
        payload = client.get(f"{API}/updates/summary", params=window(world)).json()
        assert payload["last_run"]["status"] == "partial" and payload["last_run"]["packages_checked"] == 77
        assert set(payload["last_run"]) == {
            "started_at", "finished_at", "status", "packages_checked", "packages_changed", "packages_updated",
            "packages_failed", "packages_unavailable", "events_created"}

        last_runs = payload["last_runs"]
        assert [r["ecosystem"] for r in last_runs] == sorted(r["ecosystem"] for r in last_runs)
        by_ecosystem = {r["ecosystem"]: r for r in last_runs}
        assert (by_ecosystem["npm"]["status"], by_ecosystem["npm"]["packages_checked"]) == ("success", 5)
        assert (by_ecosystem["pypi"]["status"], by_ecosystem["pypi"]["packages_checked"]) == ("partial", 77)
        assert {k: v for k, v in by_ecosystem["pypi"].items() if k != "ecosystem"} == payload["last_run"]
    finally:
        run_db(_cleanup_runs)


async def _cleanup_runs(db: AsyncSession) -> None:
    await db.execute(delete(Run).where(Run.started_at > datetime.now(UTC) + timedelta(hours=100)))


# --------------------------------------------------------------------------- /admin/sync/status


def test_sync_status_without_runs_creates_no_rows() -> None:
    """A GET reads: with an empty journal and no sync_states at all, nothing is created."""

    async def go() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as db:
                await db.execute(update(PackageEvent).values(run_id=None))
                await db.execute(delete(Run))
                await db.execute(delete(SyncState))
                status = await admin_api.sync_status(db)
                assert (status.status, status.sync_type, status.packages_synced, status.packages_total,
                        status.last_error) == ("idle", "incremental", 0, 0, None)
                assert status.last_success_at is None and status.cursor_data == {}
                assert status.last_update_run is None and status.recent_runs == []
                assert status.registries
                assert all(r.last_success_at is None and r.consecutive_failures == 0 and r.cursor_data == {}
                           for r in status.registries)
                assert await db.scalar(select(func.count(SyncState.id))) == 0
        finally:
            await engine.dispose()        # never committed: the shared journal is untouched

    asyncio.run(go())


def test_sync_status_summarises_the_latest_run(client: TestClient, world, auth: dict) -> None:
    failed = _daily(Ecosystem.pypi, 103, "failed", 3)
    failed.error = "boom"
    failed.packages_total = 9
    run_db(lambda db: _add_runs(db, _daily(Ecosystem.pypi, 101, "success", 1), failed))
    try:
        payload = client.get(f"{API}/admin/sync/status", headers=auth).json()
        assert (payload["status"], payload["sync_type"], payload["packages_synced"], payload["packages_total"],
                payload["last_error"]) == ("failed", "incremental", 3, 9, "boom")
        assert payload["recent_runs"][0]["error"] == "boom"
        assert payload["last_update_run"]["packages_checked"] == 3

        seed = Run(operation="seed", ecosystem=None, status="running", started_at=datetime.now(UTC) + timedelta(hours=104),
                   packages_checked=4, packages_total=10)
        run_db(lambda db: _add_runs(db, seed))
        payload = client.get(f"{API}/admin/sync/status", headers=auth).json()
        assert (payload["status"], payload["sync_type"], payload["packages_synced"], payload["packages_total"],
                payload["last_error"]) == ("running", "full", 4, 10, None)
    finally:
        run_db(_cleanup_runs)


def test_sync_status_registries_and_pypi_keys(client: TestClient, world, auth: dict) -> None:
    payload = client.get(f"{API}/admin/sync/status", headers=auth).json()

    async def pypi_state(db: AsyncSession) -> tuple[Any, dict]:
        rows = (await db.execute(select(SyncState.last_success_at, Registry.slug, SyncState.cursor_data)
                                 .join(Registry, Registry.id == SyncState.registry_id)
                                 .where(Registry.ecosystem == Ecosystem.pypi))).all()
        latest = max((r[0] for r in rows if r[0]), default=None)
        return latest, next((r[2] for r in rows if r[1] == "pypi"), {})

    latest, cursor = run_db(pypi_state)
    assert payload["last_success_at"] == (latest.isoformat() if latest else None)
    assert payload["cursor_data"] == cursor

    ours = [r for r in payload["registries"] if r["slug"].startswith(PREFIX)]
    assert [(r["slug"], r["ecosystem"]) for r in ours] == [
        (world.npm_hi, "npm"), (world.npm_lo, "npm"), (world.pypi2, "pypi")]
    npm_lo, pypi2 = ours[1], ours[2]
    assert (npm_lo["enabled"], npm_lo["last_error"], npm_lo["consecutive_failures"], npm_lo["cursor_data"]) == (
        False, "x", 3, {"a": 1})
    assert npm_lo["degraded_until"] is not None
    assert pypi2 == {"slug": world.pypi2, "ecosystem": "pypi", "enabled": True, "last_success_at": None,
                     "last_error": None, "consecutive_failures": 0, "degraded_until": None, "cursor_data": {}}
    ecosystems = [r["ecosystem"] for r in payload["registries"]]
    assert ecosystems == sorted(ecosystems)
