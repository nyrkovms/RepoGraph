"""POST /resolve in the graph format (IMPL C2, SPEC §9.1): HTTP on the seeded catalog, visibility by profile and the
cache check in transactions that are rolled back."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.base import NormalizedPackage, NormalizedVersion, PackageRef
from app.fetcher import pipeline
from app.models.enums import Ecosystem, PackageOrigin, RegistryProtocol
from app.models.package import Package
from app.models.registry import Registry
from app.models.source_profile import SourceProfile, SourceProfileRegistry
from app.resolver.service import resolve_graph
from app.schemas.resolve import GraphResolveRequest

PLUGIN = pipeline.plugin_for("pypi")


def test_graph_for_flask(client: TestClient) -> None:
    response = client.post("/api/v1/resolve", json={"roots": [{"purl": "pkg:pypi/flask", "constraint": ">=3"}],
                                                    "environment": {"python_version": "3.12", "os": "linux"}})
    assert response.status_code == 200
    graph = response.json()
    assert graph["error"] is None
    assert (graph["ecosystem"], graph["profile"], graph["input_mode"]) == ("pypi", "default", "requirements")
    assert graph["emulates"] == "pip 24 (resolvelib)"
    assert graph["catalog_revision"] is not None and graph["resolved_at"]
    flask = next(n for n in graph["nodes"] if n["name"] == "flask")
    assert flask["root"] and flask["depth"] == 1 and flask["registry"] == "pypi"
    assert flask["purl"] == f"pkg:pypi/flask@{flask['version']}"
    assert flask["latest_compatible"]
    names = {n["name"] for n in graph["nodes"]}
    assert {"werkzeug", "jinja2", "click"} <= names
    assert {"from": "root", "to": flask["id"], "constraint": ">=3", "type": "runtime", "details": {}} in graph["edges"]


def test_lock_mode_hides_roots_reached_from_other_roots(client: TestClient) -> None:
    roots = [{"purl": "pkg:pypi/flask"}, {"purl": "pkg:pypi/werkzeug"}]
    for mode in ("lock", "freeze"):
        graph = client.post("/api/v1/resolve", json={"roots": roots, "input_mode": mode}).json()
        assert graph["input_mode"] == "lock"
        hidden = {n["name"] for n in graph["nodes"] if n["hidden"]}
        assert hidden == {"werkzeug"}
    graph = client.post("/api/v1/resolve", json={"roots": roots}).json()
    assert not any(n["hidden"] for n in graph["nodes"])


def test_nested_format_is_still_served(client: TestClient) -> None:
    response = client.post("/api/v1/resolve", json={"dependencies": [{"name": "flask", "version_spec": ">=3"}],
                                                    "input_mode": "freeze"})
    assert response.status_code == 200
    body = response.json()
    assert "flask" in body["resolved"] and body["resolve_strategy"] == "resolvelib"
    werkzeug = next(d for d in body["resolved"]["flask"]["dependencies"] if d["name"] == "werkzeug")
    assert werkzeug["constraint"] and werkzeug["dependency_type"] == "runtime"


def test_invalid_graph_requests(client: TestClient) -> None:
    for body in (
        {"ecosystem": "maven", "roots": [{"purl": "pkg:maven/org.example/x"}]},
        {"profile": "no-such-profile", "roots": [{"purl": "pkg:pypi/flask"}]},
        {"roots": [{"purl": "pkg:npm/left-pad"}]},          # an npm root in a PyPI request
        {"roots": [{"purl": "flask"}]},
    ):
        response = client.post("/api/v1/resolve", json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_request"
    assert client.post("/api/v1/resolve", json={"roots": []}).status_code == 422


# --- profile and cache -----------------------------------------------------------------------------------------


def _in_rolled_back_transaction(test: Callable[[AsyncSession, Registry, Registry], Awaitable[None]]) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            # Savepoints: the catalog commits and rolls back inside a resolve; the test transaction must survive both.
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                pypi = await pipeline.get_or_create_pypi_registry(db)
                mirror = Registry(
                    ecosystem=pypi.ecosystem, name="Test mirror", slug=f"c2-mirror-{uuid.uuid4().hex[:8]}",
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


async def _write(db: AsyncSession, name: str, per_registry: dict[Registry, list[str]]) -> None:
    """Versions per source; "1.0:removed" gives the version that status in that source."""
    snapshots = [
        NormalizedPackage(ref=PackageRef(ecosystem="pypi", namespace="", name=name), registry_id=str(registry.id),
                          display_name=name, revision="1",
                          versions=[NormalizedVersion(version=v.split(":")[0], status=(v.split(":") + ["active"])[1],
                                                      dependencies=[]) for v in versions])
        for registry, versions in sorted(per_registry.items(), key=lambda item: item[0].priority)
    ]
    await pipeline.write_package(db, PLUGIN, snapshots, missing_registry_ids=(), origin=PackageOrigin.seed)
    await db.flush()


async def _profile(db: AsyncSession, *registries: Registry, routing: dict | None = None) -> str:
    """A profile of `registries` in this order; `routing` overrides the routing of the last one."""
    slug = f"c2-{uuid.uuid4().hex[:8]}"
    profile = SourceProfile(ecosystem=Ecosystem.pypi, slug=slug, name=slug, is_default=False)
    db.add(profile)
    await db.flush()
    for position, registry in enumerate(registries):
        db.add(SourceProfileRegistry(profile_id=profile.id, registry_id=registry.id, ecosystem=Ecosystem.pypi,
                                     position=position,
                                     routing_override=routing if position == len(registries) - 1 else None))
    await db.flush()
    return slug


def test_versions_are_chosen_through_the_profile() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, name, {pypi: ["1.0"], mirror: ["1.0", "2.0"]})
        default = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}]))
        assert [(n.version, n.registry) for n in default.nodes] == [("1.0", "pypi")]
        assert default.nodes[0].latest_compatible == "1.0"
        mirror_only = await _profile(db, mirror)
        both = await _profile(db, mirror, pypi)
        through_mirror = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}],
                                                                     profile=mirror_only))
        assert [(n.version, n.registry) for n in through_mirror.nodes] == [("2.0", mirror.slug)]

        mirror.enabled = False   # frozen: out of every profile
        await db.flush()
        frozen = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=mirror_only))
        assert frozen.error["code"] == "not_found" and frozen.nodes == []
        rest = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=both))
        assert [(n.version, n.registry) for n in rest.nodes] == [("1.0", "pypi")]

    _in_rolled_back_transaction(check)


def test_cached_answer_is_dropped_when_the_catalog_changes() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, name, {pypi: ["1.0"]})
        request = GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}])
        first = await resolve_graph(db, request)
        again = await resolve_graph(db, request)
        assert again.resolved_at == first.resolved_at          # served from the cache
        assert again.catalog_revision == first.catalog_revision

        await _write(db, name, {pypi: ["1.0", "1.1"]})
        await db.execute(update(Package).where(Package.normalized_name == name)
                         .values(synced_at=datetime.now(UTC) + timedelta(seconds=1)))
        changed = await resolve_graph(db, request)
        assert changed.resolved_at != first.resolved_at
        assert [n.version for n in changed.nodes] == ["1.1"]

    _in_rolled_back_transaction(check)


def test_node_names_the_source_where_the_version_is_active() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, name, {pypi: ["1.0"], mirror: ["1.0:removed"]})
        graph = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}],
                                                            profile=await _profile(db, mirror, pypi)))
        assert [(n.version, n.registry) for n in graph.nodes] == [("1.0", "pypi")]

        yanked = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, yanked, {pypi: ["1.0", "1.1:yanked"]})
        assert [n.version for n in (await resolve_graph(db, GraphResolveRequest(
            roots=[{"purl": f"pkg:pypi/{yanked}"}]))).nodes] == ["1.0"]
        assert [n.version for n in (await resolve_graph(db, GraphResolveRequest(
            roots=[{"purl": f"pkg:pypi/{yanked}", "constraint": "==1.1"}]))).nodes] == ["1.1"]

    _in_rolled_back_transaction(check)


def test_priority_source_with_the_package_is_its_only_source() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, name, {pypi: ["9.0"], mirror: ["1.0"]})
        graph = await resolve_graph(db, GraphResolveRequest(
            roots=[{"purl": f"pkg:pypi/{name}"}],
            profile=await _profile(db, pypi, mirror, routing={"mode": "priority"})))
        assert [(n.version, n.registry) for n in graph.nodes] == [("1.0", mirror.slug)]

    _in_rolled_back_transaction(check)


def test_package_the_profile_does_not_see_is_looked_for_once_in_its_sources(monkeypatch) -> None:
    from app.resolver import catalog as catalog_module

    asked: list[list[str]] = []

    async def absent_everywhere(db, ref, *, extra_registries=(), **_):
        asked.append([r.slug for r in extra_registries])   # the source answered 404: nothing gets linked
        return await pipeline.find_package(db, ref)

    monkeypatch.setattr(catalog_module, "sync_package", absent_everywhere)

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, name, {pypi: ["1.0"]})
        profile = await _profile(db, mirror)
        graph = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=profile))
        assert graph.error["code"] == "not_found" and asked == [[mirror.slug]]
        await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=profile,
                                                    options={"timeout_s": 29}))   # not the cached answer
        assert asked == [[mirror.slug]]   # the negative cache remembers the 404

    _in_rolled_back_transaction(check)


def test_cached_answer_is_dropped_when_the_profile_sources_change() -> None:
    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        await _write(db, name, {pypi: ["1.0"], mirror: ["2.0"]})
        profile = await _profile(db, pypi, mirror)
        request = GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=profile)
        assert [n.version for n in (await resolve_graph(db, request)).nodes] == ["2.0"]
        mirror.enabled = False   # frozen: the cached answer with 2.0 must not be served
        await db.flush()
        assert [n.version for n in (await resolve_graph(db, request)).nodes] == ["1.0"]

    _in_rolled_back_transaction(check)


def test_nested_format_rejects_unsupported_options(client: TestClient) -> None:
    for body in (
        {"ecosystem": "npm", "dependencies": [{"name": "left-pad"}]},
        {"input_mode": "lock", "dependencies": [{"name": "flask"}]},
    ):
        response = client.post("/api/v1/resolve", json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_request"


def test_answer_with_a_failed_dependency_load_is_not_cached(monkeypatch) -> None:
    async def failing(db, package, version_row):
        raise ConnectionError("upstream timed out")

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry) -> None:
        name = f"c2-test-{uuid.uuid4().hex[:10]}"
        snapshot = NormalizedPackage(ref=PackageRef(ecosystem="pypi", namespace="", name=name), registry_id=str(pypi.id),
                                     display_name=name, revision="1",
                                     versions=[NormalizedVersion(version="1.0", status="active", dependencies=None)])
        await pipeline.write_package(db, PLUGIN, [snapshot], missing_registry_ids=(), origin=PackageOrigin.seed)
        await db.commit()   # into the test transaction: the catalog's rollback must not take the package with it
        monkeypatch.setattr(pipeline, "ensure_version_dependencies", failing)
        request = GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}])
        first = await resolve_graph(db, request)
        assert first.nodes[0].errors == ["dependencies unknown"]
        again = await resolve_graph(db, request)
        assert again.nodes[0].errors == ["dependencies unknown"]
        assert again.resolved_at != first.resolved_at   # a transient failure is not kept for an hour

    _in_rolled_back_transaction(check)
