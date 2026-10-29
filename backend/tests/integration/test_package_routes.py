"""Single-package routes (IMPL C1): purl addressing, the PyPI aliases, version visibility by profile (SPEC §9.1, §6.4).

The visibility tests call the route functions in a transaction that is rolled back, so the seeded catalog the other
integration tests use stays as it is; the HTTP tests read the seeded `flask`."""

import asyncio
import uuid
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import package_detail as routes
from app.config import get_settings
from app.ecosystems.base import NormalizedDependency, NormalizedPackage, NormalizedVersion, PackageRef
from app.fetcher import pipeline
from app.models.enums import Ecosystem, PackageOrigin, RegistryProtocol
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.source_profile import SourceProfile, SourceProfileRegistry
from app.models.version import PackageVersion

PLUGIN = pipeline.plugin_for("pypi")


# --- HTTP: addressing and aliases ---------------------------------------------------------------------------


def test_new_and_alias_routes_answer_the_same(client: TestClient) -> None:
    new = client.get("/api/v1/packages/pypi/flask/-/")
    alias = client.get("/api/v1/packages/pypi/Flask")
    assert new.status_code == alias.status_code == 200
    card = new.json()
    assert card == alias.json()
    assert (card["purl"], card["namespace"], card["origin"]) == ("pkg:pypi/flask", "", "seed")
    assert [s["registry"] for s in card["sources"]] == ["pypi"]
    assert card["sources"][0]["primary"] is True

    versions = client.get("/api/v1/packages/pypi/flask/-/versions", params={"limit": 200}).json()
    assert versions == client.get("/api/v1/packages/pypi/flask/versions", params={"limit": 200}).json()
    version = versions["data"][0]["version"]

    detail = client.get(f"/api/v1/packages/pypi/flask/-/versions/{version}").json()
    assert detail == client.get(f"/api/v1/packages/pypi/flask/versions/{version}").json()
    assert detail["purl"] == f"pkg:pypi/flask@{version}"
    assert [s["registry"] for s in detail["sources"]] == ["pypi"]
    assert detail["requires_python"] == (detail["engines"] or {}).get("python")

    artifacts = client.get(f"/api/v1/packages/pypi/flask/-/versions/{version}/artifacts").json()
    assert artifacts == client.get(f"/api/v1/packages/pypi/flask/versions/{version}/artifacts").json()
    assert all(item["registry"] == "pypi" for item in artifacts["data"])

    tree = client.get(f"/api/v1/packages/pypi/flask/-/versions/{version}/dependencies", params={"depth": 1})
    assert tree.status_code == 200
    assert tree.json() == client.get(f"/api/v1/packages/pypi/flask/versions/{version}/dependencies",
                                     params={"depth": 1}).json()
    assert tree.json()["profile"] == "default"

    vulns = client.get("/api/v1/packages/pypi/flask/-/vulnerabilities")
    assert vulns.json() == client.get("/api/v1/packages/pypi/flask/vulnerabilities").json()


def test_aliases_are_deprecated_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/v1/packages/pypi/{name}"]["get"]["deprecated"] is True
    assert "deprecated" not in paths["/api/v1/packages/{ecosystem}/{coordinates}/-/"]["get"]


def test_purl_redirects_to_the_canonical_path(client: TestClient) -> None:
    package = client.get("/api/v1/purl/pkg:pypi/Flask", follow_redirects=False)
    assert package.status_code == 307
    assert package.headers["location"] == "/api/v1/packages/pypi/flask/-/"

    version = client.get("/api/v1/purl/pkg:pypi/torch@2.5.1%2Bcu121?repository_url=https://x", follow_redirects=False)
    assert version.headers["location"] == "/api/v1/packages/pypi/torch/-/versions/2.5.1%2Bcu121"

    assert client.get("/api/v1/purl/pkg:pypi/flask").json()["purl"] == "pkg:pypi/flask"


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/v1/purl/pkg:cocoapods/x", 404),          # no ecosystem for the type
        ("/api/v1/purl/pkg:npm/left-pad", 404),         # ecosystem without a plugin yet
        ("/api/v1/purl/flask", 422),                    # not a purl
        ("/api/v1/packages/npm/left-pad/-/", 404),
        ("/api/v1/packages/rubygems/rails/-/", 404),
        ("/api/v1/packages/pypi/flask/-/versions/0.0.0.does-not-exist", 404),
        ("/api/v1/packages/pypi/flask/-/versions?profile=no-such-profile", 404),
    ],
)
def test_unknown_targets(client: TestClient, path: str, status: int) -> None:
    assert client.get(path, follow_redirects=False).status_code == status


# --- visibility by profile ----------------------------------------------------------------------------------


def _in_rolled_back_transaction(test: Callable[[AsyncSession, Registry, Registry], Awaitable[None]]) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                pypi = await pipeline.get_or_create_pypi_registry(db)
                mirror = Registry(
                    ecosystem=pypi.ecosystem, name="Test mirror", slug=f"c1-mirror-{uuid.uuid4().hex[:8]}",
                    base_url="https://mirror.invalid/simple/", protocol=RegistryProtocol.pypi_simple, priority=50,
                )
                db.add(mirror)
                await db.flush()
                await test(db, pypi, mirror)
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(run())


def _name() -> str:
    return f"c1-test-{uuid.uuid4().hex[:10]}"


def _version(version: str, status: str = "active", deps: list[str] | None = None) -> NormalizedVersion:
    return NormalizedVersion(
        version=version, status=status,  # type: ignore[arg-type]
        dependencies=[
            NormalizedDependency(target=PackageRef(ecosystem="pypi", namespace="", name=d), constraint=">=1",
                                 dependency_type="runtime", raw_spec=f"{d}>=1")
            for d in deps or []
        ],
        engines={"python": ">=3.9"},
    )


async def _write(db: AsyncSession, name: str, per_registry: dict[Registry, list[NormalizedVersion]]) -> None:
    snapshots = [
        NormalizedPackage(ref=PackageRef(ecosystem="pypi", namespace="", name=name), registry_id=str(registry.id),
                          display_name=name, revision="1", versions=versions)
        for registry, versions in sorted(per_registry.items(), key=lambda item: item[0].priority)
    ]
    await pipeline.write_package(db, PLUGIN, snapshots, missing_registry_ids=(), origin=PackageOrigin.seed)
    await db.flush()


async def _profile(db: AsyncSession, *members: tuple[Registry, dict | None]) -> str:
    slug = f"c1-{uuid.uuid4().hex[:8]}"
    profile = SourceProfile(ecosystem=Ecosystem.pypi, slug=slug, name=slug, is_default=False)
    db.add(profile)
    await db.flush()
    for position, (registry, override) in enumerate(members):
        db.add(SourceProfileRegistry(profile_id=profile.id, registry_id=registry.id, ecosystem=Ecosystem.pypi,
                                     position=position, routing_override=override))
    await db.flush()
    return slug


async def _versions(db: AsyncSession, name: str, profile: str | None = None) -> dict[str, str]:
    page = await routes.list_versions("pypi", name, db, profile=profile, limit=200, cursor=None)
    return {item.version: item.status for item in page.data}


def test_default_profile_shows_only_its_sources() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = _name()
        await _write(db, name, {pypi: [_version("1.0"), _version("2.0")], mirror: [_version("2.0"), _version("3.0")]})

        # The default profile from migration 0006 holds only `pypi`.
        assert await _versions(db, name) == {"1.0": "active", "2.0": "active"}
        assert await _versions(db, name, await _profile(db, (mirror, None))) == {"2.0": "active", "3.0": "active"}
        both = await _profile(db, (pypi, None), (mirror, None))
        assert set(await _versions(db, name, both)) == {"1.0", "2.0", "3.0"}

    _in_rolled_back_transaction(check)


def test_disabled_source_is_frozen_out_of_every_profile() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = _name()
        await _write(db, name, {pypi: [_version("1.0")], mirror: [_version("3.0")]})
        both = await _profile(db, (pypi, None), (mirror, None))
        mirror.enabled = False
        await db.flush()
        assert await _versions(db, name, both) == {"1.0": "active"}

    _in_rolled_back_transaction(check)


def test_priority_source_that_has_the_package_hides_the_others() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = _name()
        await _write(db, name, {pypi: [_version("1.0"), _version("2.0")], mirror: [_version("2.0"), _version("3.0")]})
        profile = await _profile(db, (pypi, None), (mirror, {"mode": "priority"}))
        assert set(await _versions(db, name, profile)) == {"2.0", "3.0"}

        await db.execute(update(PackageRegistry).where(PackageRegistry.registry_id == mirror.id)
                         .values(missing_since=datetime.now(UTC)))
        assert set(await _versions(db, name, profile)) == {"1.0", "2.0", "3.0"}

    _in_rolled_back_transaction(check)


def test_status_is_the_best_one_among_visible_sources() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = _name()
        await _write(db, name, {pypi: [_version("2.0")], mirror: [_version("2.0", status="yanked")]})
        assert await _versions(db, name, await _profile(db, (mirror, None))) == {"2.0": "yanked"}
        assert await _versions(db, name, await _profile(db, (mirror, None), (pypi, None))) == {"2.0": "active"}

        detail = await routes.get_version("pypi", name, "2.0", db)
        assert {(s.registry, s.status) for s in detail.sources} == {("pypi", "active"), (mirror.slug, "yanked")}

    _in_rolled_back_transaction(check)


def test_unknown_profile_is_404() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = _name()
        await _write(db, name, {pypi: [_version("1.0")]})
        with pytest.raises(HTTPException) as exc:
            await _versions(db, name, "no-such-profile")
        assert exc.value.status_code == 404

    _in_rolled_back_transaction(check)


def test_versions_without_release_date_page_after_dated_ones() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = _name()
        await _write(db, name, {pypi: [_version(f"1.{i}") for i in range(5)]})
        package = await pipeline.find_package(db, PackageRef(ecosystem="pypi", namespace="", name=name))
        await db.execute(update(PackageVersion).where(PackageVersion.package_id == package.id,
                                                      PackageVersion.version.in_(["1.0", "1.1"]))
                         .values(release_date=datetime(2024, 1, 1, tzinfo=UTC)))
        seen, cursor = [], None
        while True:
            page = await routes.list_versions("pypi", name, db, profile=None, limit=2, cursor=cursor)
            seen += [item.version for item in page.data]
            if not page.has_more:
                break
            cursor = page.cursor
        assert sorted(seen) == [f"1.{i}" for i in range(5)]
        assert len(seen) == 5
        assert set(seen[:2]) == {"1.0", "1.1"}

    _in_rolled_back_transaction(check)


def test_tree_follows_the_profile() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        root, child, other = _name(), _name(), _name()
        await _write(db, child, {pypi: [_version("1.0")], mirror: [_version("1.0"), _version("2.0")]})
        await _write(db, other, {pypi: [_version("1.0")], mirror: [_version("1.0")]})
        # The sources disagree on the root's dependencies: pypi (priority 0) gives the canonical set, the mirror a variant.
        await _write(db, root, {pypi: [_version("1.0", deps=[child])], mirror: [_version("1.0", deps=[other])]})

        async def tree(profile: str | None) -> list[tuple[str, str | None]]:
            response = await routes.get_dependencies_tree("pypi", root, "1.0", db, profile=profile, depth=1,
                                                          include_optional=False, python_version=None, os=None)
            return [(node.name, node.resolved_version) for node in response.dependencies]

        assert await tree(None) == [(child, "1.0")]           # pypi's variant; child 2.0 is hidden from pypi
        assert await tree(await _profile(db, (mirror, None))) == [(other, "1.0")]

        await _write(db, root + "-x", {mirror: [_version("1.0", deps=[child])]})
        response = await routes.get_dependencies_tree("pypi", root + "-x", "1.0", db,
                                                      profile=await _profile(db, (mirror, None)), depth=1,
                                                      include_optional=False, python_version=None, os=None)
        assert [(n.name, n.resolved_version) for n in response.dependencies] == [(child, "2.0")]

    _in_rolled_back_transaction(check)


def test_tree_takes_the_variant_of_the_source_where_the_version_is_active() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        root, child, other = _name(), _name(), _name()
        await _write(db, child, {pypi: [_version("1.0")]})
        await _write(db, other, {pypi: [_version("1.0")]})
        # The mirror comes first in the profile but has removed 1.0; resolve takes pypi's copy, so does the tree.
        await _write(db, root, {pypi: [_version("1.0", deps=[child])], mirror: [_version("1.0", "removed", deps=[other])]})
        response = await routes.get_dependencies_tree("pypi", root, "1.0", db, profile=await _profile(db, (mirror, None), (pypi, None)),
                                                      depth=1, include_optional=False, python_version=None, os=None)
        assert [n.name for n in response.dependencies] == [child]

    _in_rolled_back_transaction(check)
