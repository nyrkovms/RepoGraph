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
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import admin as admin_api
from app.config import get_settings
from app.fetcher import sources
from app.models.dependency import Dependency
from app.models.enums import (
    DependenciesState,
    DependencyType,
    Ecosystem,
    PackageEventKind,
    PackageOrigin,
    PackageVersionStatus,
    RegistryProtocol,
    SyncStatus,
)
from app.models.package import Package
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.run import Run
from app.models.source_profile import SourceProfile, SourceProfileRegistry
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


def _depends(version: PackageVersion, target: Package, kind: DependencyType) -> Dependency:
    return Dependency(version_id=version.id, dependency_name=target.name, dependency_ecosystem=Ecosystem.npm,
                      version_constraint="*", dependency_type=kind, raw_spec=target.name, resolved_package_id=target.id)


async def _add_dependents(db: AsyncSession, now: datetime) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Dependents for `used_by`: ui is used by `a` (runtime + peer in one version) and `c` (peer): 2. widget is used by
    `d` only: `a` depends on it in an old version and `b` through dev. ui's own latest version depends on ui: no count."""
    registry = await db.scalar(select(Registry).where(Registry.slug == f"{PREFIX}npm-hi"))
    widget = await db.scalar(select(Package).where(Package.ecosystem == Ecosystem.npm, Package.name == f"{PREFIX}widget"))
    ui = await db.scalar(select(Package).where(Package.ecosystem == Ecosystem.npm, Package.name == f"{PREFIX}ui"))
    assert registry and widget and ui
    a, b, c, d = (_package(registry, Ecosystem.npm, f"{PREFIX}dep-{n}", origin=PackageOrigin.dependency,
                           latest=latest, now=now) for n, latest in (("a", "2.0.0"), ("b", "1.0.0"), ("c", "1.0.0"),
                                                                     ("d", "1.0.0")))
    db.add_all([a, b, c, d])
    await db.flush()

    def version(package: Package, number: str) -> PackageVersion:
        return PackageVersion(id=uuid.uuid4(), package_id=package.id, version=number, raw_metadata={},
                              sort_key=b"\x01", is_prerelease=False)

    a_old, a_new, b1, c1, d1 = version(a, "1.0.0"), version(a, "2.0.0"), version(b, "1.0.0"), version(c, "1.0.0"), \
        version(d, "1.0.0")
    ui_own = version(ui, "1.0.0")  # ui's latest version depends on ui itself
    db.add_all([a_old, a_new, b1, c1, d1, ui_own])
    await db.flush()
    db.add_all([
        _depends(a_new, ui, DependencyType.runtime), _depends(a_new, ui, DependencyType.peer),
        _depends(a_old, widget, DependencyType.runtime),
        _depends(b1, ui, DependencyType.dev), _depends(b1, widget, DependencyType.dev),
        _depends(c1, ui, DependencyType.peer),
        _depends(d1, widget, DependencyType.runtime),
        _depends(ui_own, ui, DependencyType.runtime),
    ])
    return [p.id for p in (a, b, c, d)], [v.id for v in (a_old, a_new, b1, c1, d1, ui_own)]


async def _remove_dependents(db: AsyncSession, package_ids: list[uuid.UUID], version_ids: list[uuid.UUID]) -> None:
    await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
    await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
    await db.execute(delete(Package).where(Package.id.in_(package_ids)))


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
    # A 404 for a version looks upstream first; offline that is a failed request, and the breaker must not outlive us.
    await db.execute(update(SyncState).values(consecutive_failures=0, degraded_until=None))


@pytest.fixture(scope="module")
def world():
    now = datetime.now(UTC)
    run_db(_cleanup)
    slugs = run_db(lambda db: _build(db, now))
    yield SimpleNamespace(now=now, **slugs)
    run_db(_cleanup)


@pytest.fixture
def dependents(world):
    """Apart from `world`: these share its prefix, and the search tests count everything the prefix finds."""
    package_ids, version_ids = run_db(lambda db: _add_dependents(db, world.now))
    yield
    run_db(lambda db: _remove_dependents(db, package_ids, version_ids))


RELEVANCE = "rlv-core"
RELEVANCE_NAMES = [RELEVANCE, f"{RELEVANCE}-extra", f"{RELEVANCE}-zip", f"aa-{RELEVANCE}", f"zz-{RELEVANCE}"]


@pytest.fixture
def relevance(world):
    """Names around one query, outside the `c1b-` prefix the search tests count; removed after each test."""
    async def add(db: AsyncSession) -> list[uuid.UUID]:
        registry = await db.scalar(select(Registry).where(Registry.slug == f"{PREFIX}npm-hi"))
        assert registry
        packages = [_package(registry, Ecosystem.npm, name, origin=PackageOrigin.manual, latest="1.0.0", now=world.now)
                    for name in RELEVANCE_NAMES]
        db.add_all(packages)
        return [package.id for package in packages]

    async def remove(db: AsyncSession, ids: list[uuid.UUID]) -> None:
        await db.execute(delete(Package).where(Package.id.in_(ids)))

    ids = run_db(add)
    yield
    run_db(lambda db: remove(db, ids))


UX = "ux2-"


@pytest.fixture
def usage_graph(world):
    """Usage counts for the home page and the list sort, on names outside the `c1b-` prefix (npm, latest versions):

        u1..u5 depend on old (5); u1..u3 on hub3 (3); u1, u2 on hub2 and nul (2 each); u1 on hub1 and dep (1 each).
        old: latest released five years ago. dep: deprecated. nul: `deprecation` holds JSON null, not SQL NULL.
        u1..u3 are released in the future (newest u3) so they top "latest releases"; the latest version of u4 is a
        prerelease and that of u5 is yanked: both newer still, both left out of it. u1..u5 are used by nobody."""
    now = world.now
    ids: list[uuid.UUID] = []

    async def add(db: AsyncSession) -> None:
        registry = await db.scalar(select(Registry).where(Registry.slug == f"{PREFIX}npm-hi"))
        assert registry
        packages = {name: _package(registry, Ecosystem.npm, f"{UX}{name}", origin=PackageOrigin.manual, latest="1.0.0",
                                   now=now)
                    for name in ("old", "hub3", "hub2", "hub1", "dep", "nul", "u1", "u2", "u3", "u4", "u5")}
        packages["dep"].deprecation = {"message": "use something else"}
        packages["nul"].deprecation = None   # a JSONB column stores a Python None as JSON null
        db.add_all(packages.values())
        await db.flush()
        ids.extend(p.id for p in packages.values())

        released = {"old": now - timedelta(days=5 * 365), "hub3": now - timedelta(days=10),
                    "hub2": now - timedelta(days=10), "hub1": now - timedelta(days=10),
                    "dep": now - timedelta(days=100), "nul": now - timedelta(days=100),
                    "u1": now + timedelta(days=301), "u2": now + timedelta(days=302), "u3": now + timedelta(days=303),
                    "u4": now + timedelta(days=900), "u5": now + timedelta(days=800)}
        versions = {name: PackageVersion(
            id=uuid.uuid4(), package_id=package.id, version="1.0.0", raw_metadata={}, sort_key=b"\x01",
            is_prerelease=name == "u4", release_date=released[name],
            status=PackageVersionStatus.yanked if name == "u5" else PackageVersionStatus.active)
            for name, package in packages.items()}
        db.add_all(versions.values())
        await db.flush()

        uses = {"u1": ("old", "hub3", "hub2", "hub1", "dep", "nul"), "u2": ("old", "hub3", "hub2", "nul"),
                "u3": ("old", "hub3"), "u4": ("old",), "u5": ("old",)}
        db.add_all(_depends(versions[user], packages[target], DependencyType.runtime)
                   for user, targets in uses.items() for target in targets)

    async def remove(db: AsyncSession) -> None:
        version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(ids)))).all()
        await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
        await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
        await db.execute(delete(Package).where(Package.id.in_(ids)))

    run_db(add)
    yield
    run_db(remove)


def ux_names(payload: dict) -> list[str]:
    return [row["normalized_name"].removeprefix(UX) for row in payload["data"] if row["name"].startswith(UX)]


def _walk(client: TestClient, path: str, params: dict, limit: int) -> tuple[list[dict], int]:
    rows: list[dict] = []
    cursor = None
    pages = 0
    for _ in range(30):
        page = client.get(f"{API}/{path}", params=params | {"limit": limit} | ({"cursor": cursor} if cursor else {})).json()
        rows += page["data"]
        pages += 1
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    return rows, pages


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


def test_search_ranks_exact_then_prefix_then_the_rest(client: TestClient, relevance) -> None:
    order = [row["normalized_name"] for row in client.get(f"{API}/search", params={"q": RELEVANCE}).json()["data"]]
    assert order == [RELEVANCE, f"{RELEVANCE}-extra", f"{RELEVANCE}-zip", f"aa-{RELEVANCE}", f"zz-{RELEVANCE}"]

    upper = client.get(f"{API}/search", params={"q": RELEVANCE.upper()}).json()["data"]
    assert upper[0]["normalized_name"] == RELEVANCE


def test_relevance_cursor_walks_across_ranks(client: TestClient, relevance) -> None:
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"q": RELEVANCE, "limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get(f"{API}/search", params=params).json()
        seen += [row["normalized_name"] for row in page["data"]]
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert seen == [RELEVANCE, f"{RELEVANCE}-extra", f"{RELEVANCE}-zip", f"aa-{RELEVANCE}", f"zz-{RELEVANCE}"]


def test_relevance_without_a_query_behaves_as_name(client: TestClient, world) -> None:
    by_relevance = client.get(f"{API}/packages", params={"sort": "relevance", "registry": world.npm_hi}).json()
    by_name = client.get(f"{API}/packages", params={"registry": world.npm_hi}).json()
    assert by_relevance == by_name


def test_search_by_purl_finds_the_package_and_ignores_the_version(client: TestClient, relevance) -> None:
    for q in (f"pkg:npm/{RELEVANCE}", f"pkg:npm/{RELEVANCE}@1.2.3", f"PKG:NPM/{RELEVANCE}"):
        data = client.get(f"{API}/search", params={"q": q}).json()["data"]
        assert [row["normalized_name"] for row in data] == [RELEVANCE], q


def test_search_by_purl_that_matches_nothing_is_an_empty_list(client: TestClient, relevance) -> None:
    for q in (f"pkg:npm/{RELEVANCE}-missing", f"pkg:pypi/{RELEVANCE}", "pkg:npm", "pkg:cobol/x", "pkg:rpm/rocky/bash"):
        response = client.get(f"{API}/search", params={"q": q})
        assert response.status_code == 200, q
        assert response.json() == {"data": [], "cursor": None, "has_more": False}, q


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
                             "sources_added": 1, "vulnerabilities": 0, "unavailable": 0, "prereleases": 0,
                             "attention": 1}

    pypi = client.get(f"{API}/updates/summary", params=window(world) | {"ecosystem": "pypi", "registry": world.pypi2}).json()
    assert [p["purl"] for p in pypi["packages"]] == [f"pkg:pypi/{PREFIX}widget"]
    assert pypi["totals"]["yanked"] == 1 and pypi["totals"]["deprecated"] == 0

    deprecated = client.get(f"{API}/updates/summary", params=window(world) | {"kind": "release_deprecated"}).json()
    assert deprecated["totals"]["releases"] == 0 and all(p["deprecated"] for p in deprecated["packages"])
    for bad in ({"kind": "no-such-kind"}, {"ecosystem": "cobol"}):
        assert client.get(f"{API}/updates/summary", params=window(world) | bad).status_code == 422

    empty = client.get(f"{API}/updates/summary", params=window(world) | {"registry": "c1b-no-such-registry"}).json()
    assert empty["packages"] == [] and empty["totals"]["packages"] == 0


def test_updates_summary_used_by_and_origin(client: TestClient, world, dependents) -> None:
    packages = client.get(f"{API}/updates/summary", params=window(world)).json()["packages"]
    by_purl = {p["purl"]: p for p in packages}
    ui = by_purl[f"pkg:npm/%40c1b/{PREFIX}ui"]
    widget = by_purl[f"pkg:npm/{PREFIX}widget"]
    assert (ui["used_by"], ui["origin"]) == (2, "manual")
    assert (widget["used_by"], widget["origin"]) == (1, "seed")
    assert by_purl[f"pkg:pypi/{PREFIX}widget"]["used_by"] == 0

    npm = client.get(f"{API}/updates/summary", params=window(world) | {"registry": world.npm_hi}).json()
    assert npm["totals"]["attention"] == 1
    # Flags first; the unflagged ui follows the flagged widget whatever its used_by.
    assert [p["purl"] for p in npm["packages"]] == [widget["purl"], ui["purl"]]


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


# --------------------------------------------------------------------------- the package overview

OVW = "ovw-"   # outside the `c1b-` prefix: the search tests count everything with that prefix


@pytest.fixture
def overview(world):
    """A small graph (npm, latest versions):

        root -> a, b (runtime, peer), a again as optional, opt (optional), dev (dev, not in catalog), ghost (runtime,
                not in catalog)
        a -> c, ghost2 (not in catalog);  b -> c;  c -> a  (a cycle)
        user -> root, user2;  user2 -> user  (a cycle on the dependents' side)
        loop -> loop (itself)

    root has four versions: 1.0.0 (latest) and 0.9.0 stable, 1.1.0-beta.1 a prerelease, 0.1.0 yanked and undated."""
    now = world.now
    ids: list[uuid.UUID] = []

    async def add(db: AsyncSession) -> None:
        # Versions are visible through the sources of the default profile, which does not list the test registry.
        profile = await sources.default_profile(db, "npm")
        registry = await db.scalar(select(Registry).where(Registry.id == uuid.UUID(profile.registries[0].id)))
        assert registry
        packages = {name: _package(registry, Ecosystem.npm, f"{OVW}{name}", origin=PackageOrigin.manual, latest="1.0.0",
                                   now=now) for name in ("root", "a", "b", "c", "opt", "user", "user2", "loop")}
        for name, (spdx, raw) in {"a": ("MIT", None), "b": ("MIT", "MIT License"), "c": (None, "Custom"),
                                  "opt": ("Apache-2.0", None)}.items():
            packages[name].license_spdx, packages[name].license_raw = spdx, raw
        db.add_all(packages.values())
        await db.flush()
        ids.extend(p.id for p in packages.values())

        def version(name: str, number: str, key: int, *, prerelease: bool = False, released: datetime | None = now,
                    status: PackageVersionStatus = PackageVersionStatus.active) -> PackageVersion:
            return PackageVersion(id=uuid.uuid4(), package_id=packages[name].id, version=number, raw_metadata={},
                                  sort_key=bytes([key]), is_prerelease=prerelease, release_date=released, status=status,
                                  metadata_registry_id=registry.id, dependencies_state=DependenciesState.hydrated)

        latest = {name: version(name, "1.0.0", 3) for name in packages}
        extra = [version("root", "0.9.0", 2, released=now - timedelta(days=70)),
                 version("root", "1.1.0-beta.1", 4, prerelease=True),
                 version("root", "0.1.0", 1, released=None, status=PackageVersionStatus.yanked)]
        db.add_all([*latest.values(), *extra])
        await db.flush()
        for row in [*latest.values(), *extra]:
            db.add(VersionSource(version_id=row.id, registry_id=registry.id, first_seen_at=now, status=row.status))

        def edge(src: str, dst: str, kind: DependencyType, resolved: bool = True) -> Dependency:
            return Dependency(version_id=latest[src].id, dependency_name=f"{OVW}{dst}", dependency_ecosystem=Ecosystem.npm,
                              version_constraint="*", dependency_type=kind, raw_spec=dst,
                              resolved_package_id=packages[dst].id if resolved else None)

        db.add_all([
            edge("root", "a", DependencyType.runtime), edge("root", "b", DependencyType.peer),
            edge("root", "a", DependencyType.optional), edge("root", "opt", DependencyType.optional),
            edge("root", "dev", DependencyType.dev, resolved=False),
            edge("root", "ghost", DependencyType.runtime, resolved=False),
            edge("a", "c", DependencyType.runtime), edge("a", "ghost2", DependencyType.runtime, resolved=False),
            edge("b", "c", DependencyType.runtime), edge("c", "a", DependencyType.runtime),
            edge("user", "root", DependencyType.runtime), edge("user", "user2", DependencyType.runtime),
            edge("user2", "user", DependencyType.runtime),
            edge("loop", "loop", DependencyType.runtime),
        ])

    async def remove(db: AsyncSession) -> None:
        version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(ids)))).all()
        await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
        await db.execute(delete(VersionSource).where(VersionSource.version_id.in_(version_ids)))
        await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
        await db.execute(delete(Package).where(Package.id.in_(ids)))

    run_db(add)
    yield
    run_db(remove)


def test_overview_counts_the_graph(client: TestClient, overview) -> None:
    payload = client.get(f"{API}/packages/npm/{OVW}root/-/overview").json()
    assert payload["version"] == "1.0.0"
    # a and a-as-optional count once, as direct; dev has no catalog package, ghost and ghost2 (reached through a) neither.
    assert payload["dependencies"] == {"direct": 3, "optional": 1, "dev": 1, "indirect": 1, "not_in_catalog": 2}
    # Reached: a, b, c; the cycle c -> a does not loop and root is not counted in its own dependencies.
    assert payload["used_by"] == {"direct": 1, "indirect": 1}
    assert payload["licenses"] == [{"license": "MIT", "count": 2}, {"license": "Custom", "count": 1}]


def test_dependency_tree_nodes_carry_the_license(client: TestClient, overview) -> None:
    """SPDX id, else the raw string, else null: the rule of the overview's license list; an unresolved edge has none."""
    payload = client.get(f"{API}/packages/npm/{OVW}root/-/versions/1.0.0/dependencies",
                         params={"include_optional": "true"}).json()
    top = {node["name"]: node for node in payload["dependencies"]}
    assert top[f"{OVW}a"]["license"] == "MIT"
    assert top[f"{OVW}b"]["license"] == "MIT"                 # `MIT License` is the raw string, the SPDX id wins
    assert top[f"{OVW}opt"]["license"] == "Apache-2.0"
    assert top[f"{OVW}ghost"]["license"] is None and top[f"{OVW}ghost"]["resolved_version"] is None
    below = {node["name"]: node for node in top[f"{OVW}a"]["children"]}
    assert below[f"{OVW}c"]["license"] == "Custom"            # no SPDX id: the raw string
    assert below[f"{OVW}ghost2"]["license"] is None
    cycle = below[f"{OVW}c"]["children"][0]
    assert (cycle["name"], cycle["cycle"], cycle["license"]) == (f"{OVW}a", True, "MIT")


def test_overview_of_a_leaf_and_of_a_cycle(client: TestClient, overview) -> None:
    opt = client.get(f"{API}/packages/npm/{OVW}opt/-/overview").json()
    assert opt["dependencies"] == {"direct": 0, "optional": 0, "dev": 0, "indirect": 0, "not_in_catalog": 0}
    assert opt["used_by"] == {"direct": 0, "indirect": 0}   # optional edges do not make a dependent
    assert opt["licenses"] == []

    a = client.get(f"{API}/packages/npm/{OVW}a/-/overview").json()
    # a -> c -> a: c is the only dependency reached, a itself is not counted. a is used directly by root and c; b (->c),
    # user and user2 (the user <-> user2 cycle) reach it through them.
    assert a["dependencies"] == {"direct": 2, "optional": 0, "dev": 0, "indirect": 0, "not_in_catalog": 1}
    assert a["used_by"] == {"direct": 2, "indirect": 3}


def test_overview_does_not_count_a_dependency_on_the_package_itself(client: TestClient, overview) -> None:
    payload = client.get(f"{API}/packages/npm/{OVW}loop/-/overview").json()
    assert payload["dependencies"] == {"direct": 0, "optional": 0, "dev": 0, "indirect": 0, "not_in_catalog": 0}
    assert payload["used_by"] == {"direct": 0, "indirect": 0}


def test_overview_versions_activity_and_lines(client: TestClient, world, overview) -> None:
    payload = client.get(f"{API}/packages/npm/{OVW}root/-/overview").json()
    assert payload["versions"] == {"total": 4, "prereleases": 1, "not_active": 1}
    assert len(payload["activity"]) == 12
    by_month = {item["month"]: item["releases"] for item in payload["activity"]}
    assert by_month[world.now.strftime("%Y-%m")] >= 2   # 1.0.0 and the prerelease; 0.9.0 may share the month
    assert sum(by_month.values()) == 3                  # the yanked 0.1.0 has no date
    # The yanked 0.1.0 is stable and has no date: it still makes a line, with empty dates.
    assert [(line["line"], line["latest"], line["stable_releases"]) for line in payload["lines"]] == [
        ("1.0", "1.0.0", 1), ("0.9", "0.9.0", 1), ("0.1", "0.1.0", 1)]
    assert (payload["lines"][2]["released_at"], payload["lines"][2]["started_at"]) == (None, None)
    assert payload["lines"][0]["released_at"] is not None and payload["lines"][0]["started_at"] is not None


# --------------------------------------------------------------------------- RPM: capabilities are not resolved here

RPM = "ovwrpm-"


@pytest.fixture
def rpm_overview(world):
    """rocky-9: `bash` requires the capabilities `lib` (resolved to the package `lib`, which has a license) and `ghost`
    (nothing resolved) and the dev-only `devcap`; `user` requires `bash`."""
    now = world.now
    ids: list[uuid.UUID] = []

    async def add(db: AsyncSession) -> None:
        # The default rpm profile is empty until a test fills it: this one routes its packages to a registry of its own.
        profile = await db.scalar(select(SourceProfile).where(SourceProfile.ecosystem == Ecosystem.rpm,
                                                              SourceProfile.is_default))
        assert profile is not None
        registry = _registry(f"{RPM}repo", Ecosystem.rpm, RegistryProtocol.rpm_repodata, 10)
        registry.config = {"namespace": "rocky-9", "vendor": "rocky", "arch": ["x86_64"], "releasever": "9"}
        db.add(registry)
        await db.flush()
        position = await db.scalar(select(func.coalesce(func.max(SourceProfileRegistry.position), -1) + 1)
                                   .where(SourceProfileRegistry.profile_id == profile.id))
        db.add(SourceProfileRegistry(profile_id=profile.id, registry_id=registry.id, ecosystem=Ecosystem.rpm,
                                     position=position))
        packages = {name: _package(registry, Ecosystem.rpm, f"{RPM}{name}", namespace="rocky-9",
                                   origin=PackageOrigin.manual, latest="1.0-1.el9", now=now)
                    for name in ("bash", "lib", "user")}
        packages["lib"].license_spdx = "MIT"
        db.add_all(packages.values())
        await db.flush()
        ids.extend(p.id for p in packages.values())
        latest = {}
        for name, package in packages.items():
            row = PackageVersion(id=uuid.uuid4(), package_id=package.id, version="1.0-1.el9", raw_metadata={},
                                 sort_key=b"\x01", is_prerelease=False, release_date=now, status=PackageVersionStatus.active,
                                 metadata_registry_id=registry.id, dependencies_state=DependenciesState.hydrated)
            latest[name] = row
            db.add(row)
        await db.flush()
        for row in latest.values():
            db.add(VersionSource(version_id=row.id, registry_id=registry.id, first_seen_at=now, status=row.status))

        def edge(src: str, name: str, kind: DependencyType, resolved: str | None) -> Dependency:
            return Dependency(version_id=latest[src].id, dependency_name=name, dependency_ecosystem=Ecosystem.rpm,
                              version_constraint="*", dependency_type=kind, raw_spec=name,
                              resolved_package_id=packages[resolved].id if resolved else None)

        db.add_all([edge("bash", "lib", DependencyType.runtime, "lib"), edge("bash", "ghost", DependencyType.runtime, None),
                    edge("bash", "devcap", DependencyType.dev, None), edge("user", "bash", DependencyType.runtime, "bash")])

    async def remove(db: AsyncSession) -> None:
        version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(ids)))).all()
        await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
        await db.execute(delete(VersionSource).where(VersionSource.version_id.in_(version_ids)))
        await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
        await db.execute(delete(Package).where(Package.id.in_(ids)))
        registry_ids = (await db.scalars(select(Registry.id).where(Registry.slug == f"{RPM}repo"))).all()
        await db.execute(delete(SourceProfileRegistry).where(SourceProfileRegistry.registry_id.in_(registry_ids)))
        await db.execute(delete(Registry).where(Registry.id.in_(registry_ids)))

    run_db(add)
    yield
    run_db(remove)


def test_rpm_overview_does_not_claim_what_it_cannot_count(client: TestClient, rpm_overview) -> None:
    payload = client.get(f"{API}/packages/rpm/rocky-9/{RPM}bash/-/overview").json()
    # The names are counted; which of them a catalog package answers to, and what is reached through it, is not known.
    assert payload["dependencies"] == {"direct": 2, "optional": 0, "dev": 1, "indirect": None, "not_in_catalog": None}
    assert payload["used_by"] == {"direct": 1, "indirect": None}
    assert payload["licenses"] == []
    # The same package through the rest of the card: the version counts do not depend on the graph.
    assert payload["versions"]["total"] == 1 and payload["lines"] == []


def test_overview_for_a_given_version_and_an_unknown_one(client: TestClient, overview) -> None:
    old = client.get(f"{API}/packages/npm/{OVW}root/-/overview", params={"version": "0.9.0"})
    assert old.status_code == 200 and old.json()["version"] == "0.9.0"
    assert client.get(f"{API}/packages/npm/{OVW}root/-/overview", params={"version": "9.9.9"}).status_code == 404


# --------------------------------------------------------------------------- used_by, filters and /catalog/summary


def test_list_rows_carry_used_by(client: TestClient, usage_graph) -> None:
    rows = {row["normalized_name"]: row["used_by"]
            for row in client.get(f"{API}/search", params={"q": UX, "limit": 50}).json()["data"]}
    assert (rows[f"{UX}old"], rows[f"{UX}hub3"], rows[f"{UX}hub1"], rows[f"{UX}u1"]) == (5, 3, 1, 0)
    listed = client.get(f"{API}/packages", params={"ecosystem": "npm", "limit": 200}).json()["data"]
    assert {row["used_by"] for row in listed if row["name"] == f"{UX}old"} == {5}


@pytest.mark.parametrize("path", ["search", "packages"])
def test_used_by_sort_walks_pages_with_a_cursor(client: TestClient, usage_graph, path: str) -> None:
    expected = ["old", "hub3", "hub2", "nul", "dep", "hub1", "u1", "u2", "u3", "u4", "u5"]
    params: dict = {"sort": "used_by"} | ({"q": UX} if path == "search" else {"ecosystem": "npm"})
    # /packages sees the whole npm catalog: with a small page the walk is long, so take the biggest pages there.
    rows, pages = _walk(client, path, params, limit=2 if path == "search" else 200)
    assert ux_names({"data": rows}) == expected
    assert len({row["purl"] for row in rows}) == len(rows)   # no row twice across pages
    counts = [row["used_by"] for row in rows]
    assert counts == sorted(counts, reverse=True)
    if path == "search":
        assert pages == 6


def test_used_by_cursor_belongs_to_its_sort(client: TestClient, usage_graph) -> None:
    page = client.get(f"{API}/search", params={"q": UX, "sort": "used_by", "limit": 1}).json()
    assert page["has_more"]
    assert client.get(f"{API}/search", params={"q": UX, "sort": "name", "cursor": page["cursor"]}).status_code == 422


def test_min_used_by_filter(client: TestClient, usage_graph) -> None:
    def at_least(n: int) -> list[str]:
        return ux_names(client.get(f"{API}/search", params={"q": UX, "min_used_by": n, "sort": "used_by"}).json())

    assert at_least(2) == ["old", "hub3", "hub2", "nul"]
    assert at_least(5) == ["old"]
    assert at_least(6) == []
    assert len(at_least(0)) == 11   # zero is no filter


def test_deprecated_filter_ignores_json_null(client: TestClient, usage_graph) -> None:
    async def json_null(db: AsyncSession) -> int:
        return await db.scalar(text("SELECT count(*) FROM packages WHERE name = :n AND deprecation = 'null'::jsonb"),
                               {"n": f"{UX}nul"})
    assert run_db(json_null) == 1   # the fixture really holds a JSON null, not an SQL NULL

    deprecated = ux_names(client.get(f"{API}/search", params={"q": UX, "deprecated": True}).json())
    assert deprecated == ["dep"]
    rest = ux_names(client.get(f"{API}/search", params={"q": UX, "deprecated": False, "limit": 50}).json())
    assert "dep" not in rest and {"nul", "old", "u1"} <= set(rest)
    assert ux_names(client.get(f"{API}/packages", params={"deprecated": True, "limit": 200}).json()) == ["dep"]


def test_released_before_filter_uses_the_latest_version(client: TestClient, usage_graph, world) -> None:
    def before(days: int) -> list[str]:
        moment = (world.now - timedelta(days=days)).isoformat()
        params = {"q": UX, "released_before": moment, "sort": "used_by", "limit": 50}
        return ux_names(client.get(f"{API}/search", params=params).json())

    assert before(3 * 365) == ["old"]
    assert before(50) == ["old", "nul", "dep"]   # dep and nul were released 100 days ago, the hubs 10 days ago
    assert before(6 * 365) == []
    both = client.get(f"{API}/search", params={"q": UX, "released_before": (world.now - timedelta(days=50)).isoformat(),
                                               "min_used_by": 1, "sort": "used_by"}).json()
    assert ux_names(both) == ["old", "nul", "dep"]


def test_catalog_summary(client: TestClient, usage_graph, world) -> None:
    payload = client.get(f"{API}/catalog/summary").json()

    sizes = [(row["ecosystem"], row["packages"]) for row in payload["ecosystems"]]
    assert dict(sizes)["npm"] >= 11
    assert [n for _, n in sizes] == sorted((n for _, n in sizes), reverse=True)
    assert payload["licenses"] == sorted(payload["licenses"], key=lambda row: (-row["count"], row["license"]))
    assert len(payload["licenses"]) <= 12 and all(row["license"] for row in payload["licenses"])

    most_used = ux_names({"data": payload["most_used"]})
    assert most_used == [name for name in ["old", "hub3", "hub2", "nul", "dep", "hub1"] if name in most_used]
    assert len(payload["most_used"]) <= 5 and all(row["used_by"] >= 1 for row in payload["most_used"])
    assert next(row for row in payload["most_used"] if row["name"] == f"{UX}old")["used_by"] == 5

    # The prerelease and the yanked latest versions are newer but do not count.
    latest = ux_names({"data": payload["latest_releases"]})
    assert latest[:3] == ["u3", "u2", "u1"]   # then the hubs, released ten days ago
    assert "u4" not in latest and "u5" not in latest and len(payload["latest_releases"]) <= 5
    released = [row["released_at"] for row in payload["latest_releases"]]
    assert released == sorted(released, reverse=True)

    # nul holds a JSON null: it is not deprecated, however much it is used.
    deprecated = ux_names({"data": payload["deprecated_in_use"]})
    assert deprecated == ["dep"] and len(payload["deprecated_in_use"]) <= 3

    stale = payload["stale_in_use"]
    assert stale["packages"] >= 1 and (stale["years"], stale["min_used_by"]) == (3, 5)
    cutoff = datetime.fromisoformat(stale["released_before"])
    assert timedelta(days=3 * 365 - 1) < world.now - cutoff < timedelta(days=3 * 365 + 3)
    linked = client.get(f"{API}/search", params={"q": UX, "released_before": stale["released_before"],
                                                 "min_used_by": stale["min_used_by"], "limit": 50}).json()
    assert ux_names(linked) == ["old"]


# --------------------------------------------------------------------------- dependents

DPD = "dpd-"   # outside the `c1b-` prefix: the search tests count everything with that prefix


@pytest.fixture
def dependents_graph(world):
    """npm, latest versions. `core` is used directly by d1 (runtime ^1 and peer ^2: two rows), d2 (runtime ~1.5), d3
    (provided) and indirectly by i1 (-> d1, d2), i2 (-> d2) and i3 (-> i1). Left out: dv (dev), old (an old version of
    `core` needs it, its latest does not), self (depends on itself). `core` depends on d1: a cycle."""
    now = world.now
    ids: list[uuid.UUID] = []

    async def add(db: AsyncSession) -> None:
        profile = await sources.default_profile(db, "npm")
        registry = await db.scalar(select(Registry).where(Registry.id == uuid.UUID(profile.registries[0].id)))
        assert registry
        names = ("core", "d1", "d2", "d3", "i1", "i2", "i3", "dv", "old", "self", "lonely")
        packages = {name: _package(registry, Ecosystem.npm, f"{DPD}{name}", origin=PackageOrigin.manual,
                                   latest="2.0.0" if name == "old" else "1.0.0", now=now) for name in names}
        db.add_all(packages.values())
        await db.flush()
        ids.extend(p.id for p in packages.values())

        def version(name: str, number: str) -> PackageVersion:
            return PackageVersion(id=uuid.uuid4(), package_id=packages[name].id, version=number, raw_metadata={},
                                  sort_key=b"\x01", is_prerelease=False, release_date=now,
                                  metadata_registry_id=registry.id, dependencies_state=DependenciesState.hydrated)

        latest = {name: version(name, "2.0.0" if name == "old" else "1.0.0") for name in names}
        old_old = version("old", "1.0.0")
        db.add_all([*latest.values(), old_old])
        await db.flush()
        for row in [*latest.values(), old_old]:
            db.add(VersionSource(version_id=row.id, registry_id=registry.id, first_seen_at=now))

        def edge(src: PackageVersion, dst: str, kind: DependencyType, constraint: str) -> Dependency:
            return Dependency(version_id=src.id, dependency_name=f"{DPD}{dst}", dependency_ecosystem=Ecosystem.npm,
                              version_constraint=constraint, dependency_type=kind, raw_spec=f"{dst}@{constraint}",
                              resolved_package_id=packages[dst].id)

        db.add_all([
            edge(latest["d1"], "core", DependencyType.runtime, "^1"), edge(latest["d1"], "core", DependencyType.peer, "^2"),
            edge(latest["d2"], "core", DependencyType.runtime, "~1.5"),
            edge(latest["d3"], "core", DependencyType.provided, "1.0.0"),
            edge(latest["i1"], "d1", DependencyType.runtime, "*"), edge(latest["i1"], "d2", DependencyType.runtime, "*"),
            edge(latest["i2"], "d2", DependencyType.runtime, "*"), edge(latest["i3"], "i1", DependencyType.runtime, "*"),
            edge(latest["dv"], "core", DependencyType.dev, "^1"),
            edge(old_old, "core", DependencyType.runtime, "^0"),
            edge(latest["self"], "self", DependencyType.runtime, "*"),
            edge(latest["core"], "d1", DependencyType.runtime, "*"),
        ])

    async def remove(db: AsyncSession) -> None:
        version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(ids)))).all()
        await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
        await db.execute(delete(VersionSource).where(VersionSource.version_id.in_(version_ids)))
        await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
        await db.execute(delete(Package).where(Package.id.in_(ids)))

    run_db(add)
    yield
    run_db(remove)


def _dependents_path(name: str) -> str:
    return f"{API}/packages/npm/{DPD}{name}/-/dependents"


def _dpd(rows: list[dict]) -> set[str]:
    return {row["name"].removeprefix(DPD) for row in rows}


def test_dependents_split_direct_and_indirect(client: TestClient, dependents_graph) -> None:
    payload = client.get(_dependents_path("core")).json()
    assert payload["totals"] == {"direct": 3, "indirect": 3}
    assert payload["cursor"] is None and payload["has_more"] is False

    rows = payload["data"]
    assert [row["relation"] for row in rows] == ["direct"] * 3 + ["indirect"] * 3   # direct first
    assert _dpd(rows[:3]) == {"d1", "d2", "d3"} and _dpd(rows[3:]) == {"i1", "i2", "i3"}   # none of dv, old, self
    by_name = {row["name"].removeprefix(DPD): row for row in rows}
    assert by_name["d1"]["purl"] == f"pkg:npm/{DPD}d1" and by_name["d1"]["latest_version"] == "1.0.0"
    assert by_name["d1"]["ecosystem"] == "npm" and by_name["d1"]["namespace"] == ""
    assert by_name["d1"]["normalized_name"] == f"{DPD}d1"
    assert by_name["i1"]["dependency_types"] == [] and by_name["i1"]["requires"] == []


def test_dependents_requires_lists_every_row_of_a_dependent(client: TestClient, dependents_graph) -> None:
    by_name = {row["name"].removeprefix(DPD): row
               for row in client.get(_dependents_path("core"), params={"relation": "direct"}).json()["data"]}
    assert (by_name["d1"]["requires"], by_name["d1"]["dependency_types"]) == (["^1", "^2"], ["peer", "runtime"])
    assert (by_name["d2"]["requires"], by_name["d2"]["dependency_types"]) == (["~1.5"], ["runtime"])
    assert (by_name["d3"]["requires"], by_name["d3"]["dependency_types"]) == (["1.0.0"], ["provided"])


def test_dependents_relation_filter_keeps_the_totals(client: TestClient, dependents_graph) -> None:
    direct = client.get(_dependents_path("core"), params={"relation": "direct"}).json()
    indirect = client.get(_dependents_path("core"), params={"relation": "indirect"}).json()
    assert _dpd(direct["data"]) == {"d1", "d2", "d3"} and _dpd(indirect["data"]) == {"i1", "i2", "i3"}
    assert direct["totals"] == indirect["totals"] == {"direct": 3, "indirect": 3}
    assert client.get(_dependents_path("core"), params={"relation": "both"}).status_code == 422


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
def test_dependents_cursor_walks_across_the_boundary(client: TestClient, dependents_graph, limit: int) -> None:
    whole = client.get(_dependents_path("core")).json()["data"]
    rows, pages = _walk(client, _dependents_path("core").removeprefix(f"{API}/"), {}, limit=limit)
    assert [row["purl"] for row in rows] == [row["purl"] for row in whole]   # the same order, no row twice or missed
    assert pages == -(-6 // limit)


def test_dependents_cursor_belongs_to_the_route(client: TestClient, dependents_graph) -> None:
    page = client.get(_dependents_path("core"), params={"limit": 1}).json()
    assert page["has_more"] and page["cursor"]
    assert client.get(_dependents_path("core"), params={"cursor": "!!!"}).status_code == 422


def test_dependents_of_a_package_nobody_uses_and_of_an_unknown_one(client: TestClient, dependents_graph) -> None:
    payload = client.get(_dependents_path("lonely")).json()
    assert payload == {"totals": {"direct": 0, "indirect": 0}, "data": [], "cursor": None, "has_more": False}
    self_user = client.get(_dependents_path("self")).json()
    assert self_user["totals"] == {"direct": 0, "indirect": 0}   # an edge to itself is no dependent
    assert client.get(f"{API}/packages/nosuch/{DPD}nope/-/dependents").status_code == 404


@pytest.mark.parametrize("name", ["core", "d1", "d2", "i1", "lonely", "self", "old"])
def test_dependents_totals_equal_the_overview(client: TestClient, dependents_graph, name: str) -> None:
    totals = client.get(_dependents_path(name)).json()["totals"]
    used_by = client.get(f"{API}/packages/npm/{DPD}{name}/-/overview").json()["used_by"]
    assert totals == used_by


# --------------------------------------------------------------------------- the versions list: line, prerelease, not_active

VSN = "vsn-"   # outside the `c1b-` prefix: the search tests count everything with that prefix


@pytest.fixture
def versions_graph(world):
    """One npm package, `vsn-pkg`, newest release first:

        2.2.0-beta.1 (prerelease), 2.1.2-rc.1 (prerelease), 2.1.1, 2.1.0, 2.0.5 (stored active, its source says yanked),
        1.9.0 (stored yanked, its source says active), 1.8.0 (deprecated in both), 1.0.0 (stable, undated)."""
    now = world.now
    package_ids: list[uuid.UUID] = []

    async def add(db: AsyncSession) -> None:
        profile = await sources.default_profile(db, "npm")
        registry = await db.scalar(select(Registry).where(Registry.id == uuid.UUID(profile.registries[0].id)))
        assert registry
        package = _package(registry, Ecosystem.npm, f"{VSN}pkg", origin=PackageOrigin.manual, latest="2.1.1", now=now)
        db.add(package)
        await db.flush()
        package_ids.append(package.id)
        active, yanked = PackageVersionStatus.active, PackageVersionStatus.yanked
        rows = [  # version, prerelease, days ago, stored status, source status
            ("2.2.0-beta.1", True, 1, active, active), ("2.1.2-rc.1", True, 2, active, active),
            ("2.1.1", False, 3, active, active), ("2.1.0", False, 4, active, active),
            ("2.0.5", False, 10, active, yanked), ("1.9.0", False, 20, yanked, active),
            ("1.8.0", False, 30, PackageVersionStatus.deprecated, PackageVersionStatus.deprecated),
            ("1.0.0", False, None, active, active),
        ]
        for number, prerelease, days, stored, in_source in rows:
            row = PackageVersion(id=uuid.uuid4(), package_id=package.id, version=number, raw_metadata={},
                                 sort_key=number.encode(), is_prerelease=prerelease, status=stored,
                                 release_date=now - timedelta(days=days) if days is not None else None,
                                 metadata_registry_id=registry.id, dependencies_state=DependenciesState.hydrated)
            db.add(row)
            await db.flush()
            db.add(VersionSource(version_id=row.id, registry_id=registry.id, first_seen_at=now, status=in_source))

    async def remove(db: AsyncSession) -> None:
        version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(package_ids)))).all()
        await db.execute(delete(VersionSource).where(VersionSource.version_id.in_(version_ids)))
        await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
        await db.execute(delete(Package).where(Package.id.in_(package_ids)))

    run_db(add)
    yield
    run_db(remove)


VSN_PATH = f"{API}/packages/npm/{VSN}pkg/-/versions"


def _versions(client: TestClient, **params: Any) -> dict:
    return client.get(VSN_PATH, params=params).json()


def _numbers(payload: dict) -> list[str]:
    return [row["version"] for row in payload["data"]]


def test_versions_rows_carry_line_and_prerelease(client: TestClient, versions_graph) -> None:
    rows = {row["version"]: row for row in _versions(client)["data"]}
    assert (rows["2.1.1"]["line"], rows["2.1.1"]["is_prerelease"]) == ("2.1", False)
    assert (rows["2.2.0-beta.1"]["line"], rows["2.2.0-beta.1"]["is_prerelease"]) == ("2.2", True)
    assert client.get(f"{VSN_PATH}/2.1.1").json()["line"] == "2.1"


def test_versions_line_returns_the_whole_line_and_ignores_the_page(client: TestClient, versions_graph) -> None:
    payload = _versions(client, line="2.1", limit=1)
    assert _numbers(payload) == ["2.1.2-rc.1", "2.1.1", "2.1.0"]
    assert payload["cursor"] is None and payload["has_more"] is False
    assert _numbers(_versions(client, line="2.1", prerelease="false")) == ["2.1.1", "2.1.0"]
    assert _numbers(_versions(client, line="2.1", prerelease="true")) == ["2.1.2-rc.1"]
    assert _numbers(_versions(client, line="2.0")) == ["2.0.5"]
    assert _versions(client, line="9.9")["data"] == []


def test_versions_prerelease_filter_and_cursor(client: TestClient, versions_graph) -> None:
    assert _numbers(_versions(client, prerelease="true")) == ["2.2.0-beta.1", "2.1.2-rc.1"]
    stable = ["2.1.1", "2.1.0", "2.0.5", "1.9.0", "1.8.0", "1.0.0"]
    assert _numbers(_versions(client, prerelease="false")) == stable
    rows, pages = _walk(client, f"packages/npm/{VSN}pkg/-/versions", {"prerelease": "false"}, 2)
    assert [row["version"] for row in rows] == stable and pages == 3


def test_versions_not_active_is_the_effective_status(client: TestClient, versions_graph) -> None:
    flagged = _versions(client, not_active="true")
    # 2.0.5 is yanked in its source though stored active, 1.9.0 the other way round.
    assert _numbers(flagged) == ["2.0.5", "1.8.0"]
    assert [row["status"] for row in flagged["data"]] == ["yanked", "deprecated"]
    assert "1.9.0" in _numbers(_versions(client, not_active="false"))
    rows, _ = _walk(client, f"packages/npm/{VSN}pkg/-/versions", {"not_active": "true"}, 1)
    assert [row["version"] for row in rows] == ["2.0.5", "1.8.0"]


def test_overview_not_active_agrees_with_the_versions_filter(client: TestClient, versions_graph) -> None:
    counts = client.get(f"{API}/packages/npm/{VSN}pkg/-/overview").json()["versions"]
    assert counts == {"total": 8, "prereleases": 2, "not_active": len(_versions(client, not_active="true")["data"])}


# --------------------------------------------------------------------------- the dependency comparison of two versions

CMP = "cmp-"   # outside the `c1b-` prefix: the search tests count everything with that prefix
CMP_PATH = f"{API}/packages/npm/{CMP}pkg/-/versions/compare"


@pytest.fixture
def compare_graph(world):
    """One npm package, `cmp-pkg`, oldest first: 0.5.0 (dependencies unavailable), 1.0.0, 1.1.0-rc.1 (prerelease),
    1.2.0 (its source says yanked), 2.0.0 (latest). Dependencies by version:

        1.0.0: core ^1 (runtime), same ^1 (runtime), gone ^1 (runtime), moved ^1 (runtime)
        1.1.0-rc.1: core ^9 (runtime)
        1.2.0: core ^2, same ^1, fresh ^1 (runtime), moved ^1 (dev), several ^1 and ^2 in two rows (runtime)
        2.0.0: as 1.2.0"""
    now = world.now
    package_ids: list[uuid.UUID] = []

    async def add(db: AsyncSession) -> None:
        profile = await sources.default_profile(db, "npm")
        registry = await db.scalar(select(Registry).where(Registry.id == uuid.UUID(profile.registries[0].id)))
        assert registry
        package = _package(registry, Ecosystem.npm, f"{CMP}pkg", origin=PackageOrigin.manual, latest="2.0.0", now=now)
        db.add(package)
        await db.flush()
        package_ids.append(package.id)
        runtime, dev = DependencyType.runtime, DependencyType.dev
        newer = [("core", "^2", runtime), ("same", "^1", runtime), ("fresh", "^1", runtime), ("moved", "^1", dev),
                 ("several", "^1", runtime), ("several", "^2", runtime)]
        rows = [  # version, key, prerelease, source status, dependencies state, dependencies
            ("0.5.0", 1, False, "active", DependenciesState.unavailable, []),
            ("1.0.0", 2, False, "active", DependenciesState.hydrated,
             [("core", "^1", runtime), ("same", "^1", runtime), ("gone", "^1", runtime), ("moved", "^1", runtime)]),
            ("1.1.0-rc.1", 3, True, "active", DependenciesState.hydrated, [("core", "^9", runtime)]),
            ("1.2.0", 4, False, "yanked", DependenciesState.hydrated, newer),
            ("2.0.0", 5, False, "active", DependenciesState.hydrated, newer),
        ]
        for number, key, prerelease, source_status, state, deps in rows:
            status = PackageVersionStatus(source_status)
            row = PackageVersion(id=uuid.uuid4(), package_id=package.id, version=number, raw_metadata={},
                                 sort_key=bytes([key]), is_prerelease=prerelease, status=PackageVersionStatus.active,
                                 release_date=now - timedelta(days=100 - key), metadata_registry_id=registry.id,
                                 dependencies_state=state)
            db.add(row)
            await db.flush()
            db.add(VersionSource(version_id=row.id, registry_id=registry.id, first_seen_at=now, status=status))
            db.add_all(Dependency(version_id=row.id, dependency_name=name, dependency_ecosystem=Ecosystem.npm,
                                  version_constraint=constraint, dependency_type=kind, raw_spec=f"{name}@{constraint}")
                       for name, constraint, kind in deps)

    async def remove(db: AsyncSession) -> None:
        version_ids = (await db.scalars(select(PackageVersion.id).where(PackageVersion.package_id.in_(package_ids)))).all()
        await db.execute(delete(Dependency).where(Dependency.version_id.in_(version_ids)))
        await db.execute(delete(VersionSource).where(VersionSource.version_id.in_(version_ids)))
        await db.execute(delete(PackageVersion).where(PackageVersion.id.in_(version_ids)))
        await db.execute(delete(Package).where(Package.id.in_(package_ids)))

    run_db(add)
    yield
    run_db(remove)


def test_compare_defaults_to_the_latest_and_the_nearest_lower_stable(client: TestClient, compare_graph) -> None:
    payload = client.get(CMP_PATH).json()
    # 2.0.0 against 1.2.0: the prerelease 1.1.0-rc.1 is not a candidate, and 1.2.0 is lower than 2.0.0.
    assert payload["from"]["version"] == "1.2.0" and payload["to"]["version"] == "2.0.0"
    assert payload["from"]["status"] == "yanked"      # effective status, the stored one is active
    assert payload["to"]["status"] == "active"
    assert payload["dependencies"] == {"added": [], "removed": [], "changed": [], "unchanged": 5}
    assert payload["dependencies_unknown"] is False


def test_compare_default_from_skips_prereleases(client: TestClient, compare_graph) -> None:
    payload = client.get(CMP_PATH, params={"to": "1.2.0"}).json()
    assert payload["from"]["version"] == "1.0.0"


def test_compare_explicit_versions(client: TestClient, compare_graph) -> None:
    payload = client.get(CMP_PATH, params={"from": "1.0.0", "to": "2.0.0"}).json()
    deps = payload["dependencies"]
    assert deps["changed"] == [{"name": "core", "dependency_type": "runtime", "from": "^1", "to": "^2"}]
    assert deps["added"] == [{"name": "moved", "constraint": "^1", "dependency_type": "dev"},
                             {"name": "fresh", "constraint": "^1", "dependency_type": "runtime"},
                             {"name": "several", "constraint": "^1, ^2", "dependency_type": "runtime"}]
    assert deps["removed"] == [{"name": "gone", "constraint": "^1", "dependency_type": "runtime"},
                               {"name": "moved", "constraint": "^1", "dependency_type": "runtime"}]
    assert deps["unchanged"] == 1                      # same


def test_compare_with_a_prerelease_given_explicitly(client: TestClient, compare_graph) -> None:
    payload = client.get(CMP_PATH, params={"from": "1.1.0-rc.1", "to": "1.0.0"}).json()
    assert payload["dependencies"]["changed"] == [
        {"name": "core", "dependency_type": "runtime", "from": "^9", "to": "^1"}]


def test_compare_unknown_dependencies(client: TestClient, compare_graph) -> None:
    payload = client.get(CMP_PATH, params={"from": "0.5.0", "to": "1.0.0"}).json()
    assert payload["dependencies_unknown"] is True
    assert payload["dependencies"] == {"added": [], "removed": [], "changed": [], "unchanged": 0}
    assert payload["from"]["version"] == "0.5.0"


def test_compare_the_first_version_has_nothing_to_compare_with(client: TestClient, compare_graph) -> None:
    payload = client.get(CMP_PATH, params={"to": "0.5.0"}).json()
    assert payload["from"] is None and payload["to"]["version"] == "0.5.0"
    assert payload["dependencies"] == {"added": [], "removed": [], "changed": [], "unchanged": 0}
    assert payload["dependencies_unknown"] is False


def test_compare_unknown_version_or_ecosystem_is_404(client: TestClient, compare_graph) -> None:
    assert client.get(CMP_PATH, params={"to": "9.9.9"}).status_code == 404
    assert client.get(CMP_PATH, params={"from": "9.9.9"}).status_code == 404
    # An unknown ecosystem: an unknown package would be fetched lazily from the upstream and count against its breaker.
    assert client.get(f"{API}/packages/nosuch/{CMP}pkg/-/versions/compare").status_code == 404


def test_compare_is_not_taken_for_a_version(client: TestClient, compare_graph) -> None:
    response = client.get(CMP_PATH)
    assert response.status_code == 200 and "dependencies" in response.json()
    assert client.get(f"{CMP_PATH[:-len('/compare')]}/2.0.0").json()["version"] == "2.0.0"
