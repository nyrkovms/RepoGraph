"""Sources and profiles API, CLI import and the fetch_packages task (IMPL B3) against the integration database.

The database is shared and not rolled back between tests, so everything created here carries the slug prefix
`b3-` and is removed afterwards. Nothing talks to the network: `test_connection` is patched.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import admin as admin_api
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.ecosystems.pypi.adapters import WarehouseAdapter
from app.models.enums import Ecosystem, RegistryProtocol, SyncStatus
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.source_profile import SourceProfile, SourceProfileRegistry
from app.models.sync_failure import SyncFailure
from app.models.sync_state import SyncState
from app.services import registries as service
from app.tasks import fetch_packages as fetch_task

PREFIX = "b3-"


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


async def _cleanup(db: AsyncSession) -> None:
    ids = (await db.scalars(select(Registry.id).where(Registry.slug.like(f"{PREFIX}%")))).all()
    await db.execute(delete(PackageRegistry).where(PackageRegistry.registry_id.in_(ids)))
    await db.execute(delete(SourceProfileRegistry).where(SourceProfileRegistry.registry_id.in_(ids)))
    profile_ids = (await db.scalars(select(SourceProfile.id).where(SourceProfile.slug.like(f"{PREFIX}%")))).all()
    await db.execute(delete(SourceProfileRegistry).where(SourceProfileRegistry.profile_id.in_(profile_ids)))
    await db.execute(delete(SourceProfile).where(SourceProfile.id.in_(profile_ids)))
    await db.execute(delete(SyncState).where(SyncState.registry_id.in_(ids)))
    await db.execute(delete(SyncFailure).where(SyncFailure.package_ref.like(f"pkg:pypi/{PREFIX}%")))
    await db.execute(delete(Registry).where(Registry.id.in_(ids)))


@pytest.fixture(autouse=True)
def tidy():
    """Remove what a test created, and put back the default profiles it switched away from."""
    defaults = run_db(lambda db: _select_defaults(db))
    yield
    async def restore(db: AsyncSession) -> None:
        await _cleanup(db)
        for ecosystem, slug in defaults:
            await db.execute(
                update(SourceProfile).where(SourceProfile.ecosystem == ecosystem, SourceProfile.slug == slug)
                .values(is_default=True)
            )
    run_db(restore)


async def _select_defaults(db: AsyncSession) -> list[tuple[Ecosystem, str]]:
    rows = await db.execute(select(SourceProfile.ecosystem, SourceProfile.slug).where(SourceProfile.is_default))
    return [(e, s) for e, s in rows.all()]


@pytest.fixture
def auth(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


def new_slug() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:8]}"


def body(slug: str | None = None, **overrides: Any) -> dict:
    return {
        "slug": slug or new_slug(), "name": "Test source", "ecosystem": "pypi", "protocol": "pypi-warehouse",
        "base_url": "https://mirror.invalid/", **overrides,
    }


def create(client: TestClient, auth: dict, **overrides: Any) -> dict:
    response = client.post("/api/v1/admin/registries", headers=auth, json=body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def patch_connection(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> list[int]:
    calls: list[int] = []

    async def fake(self: WarehouseAdapter) -> None:
        calls.append(1)
        if error is not None:
            raise error

    monkeypatch.setattr(WarehouseAdapter, "test_connection", fake)
    return calls


def insert_foreign_registry(ecosystem: Ecosystem, protocol: RegistryProtocol) -> str:
    slug = new_slug()
    run_db(lambda db: _add(db, Registry(ecosystem=ecosystem, name="Foreign", slug=slug, base_url="https://x.invalid/",
                                         protocol=protocol, priority=1, enabled=False)))
    return slug


async def _add(db: AsyncSession, row: object) -> None:
    db.add(row)


def link_packages(slug: str, present: int, missing: int) -> None:
    async def work(db: AsyncSession) -> None:
        registry = await db.scalar(select(Registry).where(Registry.slug == slug))
        packages = (await db.scalars(select(Package).where(Package.ecosystem == Ecosystem.pypi).limit(present + missing))).all()
        assert len(packages) == present + missing, "the integration database needs seeded packages"
        now = datetime.now(UTC)
        for index, package in enumerate(packages):
            db.add(PackageRegistry(
                package_id=package.id, registry_id=registry.id, ecosystem=Ecosystem.pypi, first_seen_at=now,
                missing_since=now if index >= present else None,
            ))

    run_db(work)


# --- reading -----------------------------------------------------------------------------------------


def test_list_and_get_show_breaker_state_and_package_count(client: TestClient, auth: dict) -> None:
    healthy = create(client, auth, priority=7)["slug"]
    broken = create(client, auth, priority=3, routing={"mode": "exclusive", "packages": ["torch"]})["slug"]
    link_packages(broken, present=1, missing=1)

    async def trip(db: AsyncSession) -> None:
        registry = await db.scalar(select(Registry).where(Registry.slug == broken))
        db.add(SyncState(registry_id=registry.id, status=SyncStatus.failed, last_error="HTTP 503", consecutive_failures=5,
                         degraded_until=datetime.now(UTC) + timedelta(minutes=10), cursor_data={"last_serial": 42}))

    run_db(trip)

    one = client.get(f"/api/v1/registries/{broken}")
    assert one.status_code == 200
    payload = one.json()
    assert payload["routing"] == {"mode": "exclusive", "packages": ["torch"]}
    assert payload["packages"] == 1
    assert payload["state"]["degraded"] is True
    assert payload["state"]["consecutive_failures"] == 5
    assert payload["state"]["last_error"] == "HTTP 503"
    assert payload["state"]["status"] == "failed"
    assert payload["state"]["cursor_data"] == {"last_serial": 42}
    assert payload["state"]["degraded_until"] is not None

    quiet = client.get(f"/api/v1/registries/{healthy}").json()
    assert quiet["state"] is None
    assert quiet["packages"] == 0

    listed = client.get("/api/v1/registries", params={"ecosystem": "pypi"}).json()["data"]
    slugs = [row["slug"] for row in listed]
    assert slugs.index(broken) < slugs.index(healthy)  # priority 3 before 7
    assert [(r["ecosystem"], r["priority"], r["slug"]) for r in listed] == sorted(
        (r["ecosystem"], r["priority"], r["slug"]) for r in listed
    )


def test_elapsed_breaker_is_not_degraded(client: TestClient, auth: dict) -> None:
    slug = create(client, auth)["slug"]

    async def work(db: AsyncSession) -> None:
        registry = await db.scalar(select(Registry).where(Registry.slug == slug))
        db.add(SyncState(registry_id=registry.id, status=SyncStatus.idle, consecutive_failures=5,
                         degraded_until=datetime.now(UTC) - timedelta(minutes=1)))

    run_db(work)
    assert client.get(f"/api/v1/registries/{slug}").json()["state"]["degraded"] is False


def test_get_unknown_registry_and_bad_ecosystem_filter(client: TestClient) -> None:
    assert client.get("/api/v1/registries/b3-nope").status_code == 404
    assert client.get("/api/v1/registries", params={"ecosystem": "cobol"}).status_code == 422
    assert client.get("/api/v1/profiles", params={"ecosystem": "cobol"}).status_code == 422


def test_list_filters_by_ecosystem(client: TestClient, auth: dict) -> None:
    pypi = create(client, auth)["slug"]
    npm = insert_foreign_registry(Ecosystem.npm, RegistryProtocol.npm_registry)
    npm_slugs = [r["slug"] for r in client.get("/api/v1/registries", params={"ecosystem": "npm"}).json()["data"]]
    pypi_slugs = [r["slug"] for r in client.get("/api/v1/registries", params={"ecosystem": "pypi"}).json()["data"]]
    assert npm in npm_slugs and pypi not in npm_slugs
    assert pypi in pypi_slugs and npm not in pypi_slugs


# --- create / update ---------------------------------------------------------------------------------


def test_create_is_always_disabled_and_stores_the_fields(client: TestClient, auth: dict) -> None:
    created = create(client, auth, enabled=True, priority=20, config={"negative_cache_ttl_seconds": 60},
                     auth_type="bearer", credentials_ref="MIRROR_TOKEN")
    assert created["enabled"] is False
    assert (created["priority"], created["auth_type"], created["credentials_ref"]) == (20, "bearer", "MIRROR_TOKEN")
    assert created["config"] == {"negative_cache_ttl_seconds": 60}
    assert created["state"] is None and created["packages"] == 0
    assert client.get(f"/api/v1/registries/{created['slug']}").json()["enabled"] is False


def test_create_defaults(client: TestClient, auth: dict) -> None:
    created = create(client, auth)
    assert (created["priority"], created["config"], created["routing"], created["auth_type"]) == (100, {}, None, "none")
    assert created["credentials_ref"] is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"protocol": "gopher"}, "unknown protocol"),
        ({"protocol": "pypi-simple"}, "no adapter for protocol"),
        ({"ecosystem": "npm", "protocol": "npm-registry"}, "no plugin is registered"),
        ({"routing": {"mode": "sometimes", "packages": ["torch"]}}, "unknown routing mode"),
        ({"routing": {"mode": "exclusive"}}, "needs routing.packages"),
        ({"auth_type": "bearer"}, "credentials_ref is required"),
        ({"auth_type": "kerberos", "credentials_ref": "X"}, "auth_type must be one of"),
    ],
)
def test_create_validation_errors_are_422_with_a_message(
    client: TestClient, auth: dict, overrides: dict, message: str
) -> None:
    slug = new_slug()
    response = client.post("/api/v1/admin/registries", headers=auth, json=body(slug, **overrides))
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert client.get(f"/api/v1/registries/{slug}").status_code == 404


def test_create_with_a_malformed_body_is_422(client: TestClient, auth: dict) -> None:
    response = client.post("/api/v1/admin/registries", headers=auth, json={"slug": "Bad Slug", "name": "x"})
    assert response.status_code == 422


def test_create_duplicate_slug_is_409(client: TestClient, auth: dict) -> None:
    slug = create(client, auth, name="First")["slug"]
    response = client.post("/api/v1/admin/registries", headers=auth, json=body(slug, name="Second"))
    assert response.status_code == 409
    assert client.get(f"/api/v1/registries/{slug}").json()["name"] == "First"


def test_update_changes_only_the_sent_fields(client: TestClient, auth: dict) -> None:
    created = create(client, auth, priority=50, auth_type="basic", credentials_ref="MIRROR_BASIC")
    slug = created["slug"]
    response = client.put(f"/api/v1/admin/registries/{slug}", headers=auth, json={
        "priority": 5, "routing": {"mode": "block", "packages": ["torch"]}, "slug": slug, "ecosystem": "pypi",
        "protocol": "pypi-warehouse",
    })
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["priority"] == 5
    assert updated["routing"] == {"mode": "block", "packages": ["torch"]}
    assert (updated["name"], updated["base_url"], updated["credentials_ref"]) == ("Test source", "https://mirror.invalid/", "MIRROR_BASIC")

    cleared = client.put(f"/api/v1/admin/registries/{slug}", headers=auth, json={"routing": None, "auth_type": "none"})
    assert cleared.json()["routing"] is None
    assert cleared.json()["auth_type"] == "none"


def test_update_rejects_immutable_fields_and_bad_values(client: TestClient, auth: dict) -> None:
    slug = create(client, auth)["slug"]
    for payload, message in (
        ({"slug": new_slug()}, "slug cannot be changed"),
        ({"ecosystem": "npm"}, "ecosystem cannot be changed"),
        ({"protocol": "pypi-simple"}, "protocol cannot be changed"),
        ({"routing": {"mode": "nope", "packages": ["a"]}}, "unknown routing mode"),
        ({"auth_type": "bearer"}, "credentials_ref is required"),
        ({"priority": None}, "priority cannot be null"),
    ):
        response = client.put(f"/api/v1/admin/registries/{slug}", headers=auth, json=payload)
        assert response.status_code == 422, payload
        assert message in response.json()["detail"], payload
    unchanged = client.get(f"/api/v1/registries/{slug}").json()
    assert (unchanged["priority"], unchanged["routing"], unchanged["auth_type"]) == (100, None, "none")


def test_update_cannot_enable_and_unknown_slug_is_404(client: TestClient, auth: dict) -> None:
    slug = create(client, auth)["slug"]
    response = client.put(f"/api/v1/admin/registries/{slug}", headers=auth, json={"enabled": True, "name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["name"] == "Renamed"
    assert client.put("/api/v1/admin/registries/b3-nope", headers=auth, json={"name": "x"}).status_code == 404


# --- test / enable / disable ---------------------------------------------------------------------------


def test_connection_test_reports_ok_and_failure(client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    slug = create(client, auth)["slug"]
    calls = patch_connection(monkeypatch)
    ok = client.post(f"/api/v1/admin/registries/{slug}/test", headers=auth)
    assert ok.status_code == 200
    assert ok.json()["ok"] is True and ok.json()["error"] is None and ok.json()["elapsed_ms"] >= 0
    assert calls == [1]

    patch_connection(monkeypatch, ConnectionError("connection refused"))
    failed = client.post(f"/api/v1/admin/registries/{slug}/test", headers=auth)
    assert failed.status_code == 200
    assert failed.json()["ok"] is False
    assert failed.json()["error"] == "ConnectionError: connection refused"
    assert client.post("/api/v1/admin/registries/b3-nope/test", headers=auth).status_code == 404


def test_enable_is_blocked_by_a_failed_test_and_allowed_by_a_passing_one(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = create(client, auth)["slug"]

    patch_connection(monkeypatch, TimeoutError("slow"))
    blocked = client.post(f"/api/v1/admin/registries/{slug}/enable", headers=auth)
    assert blocked.status_code == 409
    assert "TimeoutError: slow" in blocked.json()["detail"]
    assert client.get(f"/api/v1/registries/{slug}").json()["enabled"] is False

    patch_connection(monkeypatch)
    enabled = client.post(f"/api/v1/admin/registries/{slug}/enable", headers=auth)
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert client.get(f"/api/v1/registries/{slug}").json()["enabled"] is True

    disabled = client.post(f"/api/v1/admin/registries/{slug}/disable", headers=auth)
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert client.post("/api/v1/admin/registries/b3-nope/disable", headers=auth).status_code == 404
    assert client.post("/api/v1/admin/registries/b3-nope/enable", headers=auth).status_code == 404


# --- delete -------------------------------------------------------------------------------------------


def test_delete_is_blocked_by_packages_and_allowed_without(client: TestClient, auth: dict) -> None:
    held = create(client, auth)["slug"]
    link_packages(held, present=0, missing=1)  # a source a package went missing from still holds the link
    blocked = client.delete(f"/api/v1/admin/registries/{held}", headers=auth)
    assert blocked.status_code == 409
    assert "disable it" in blocked.json()["detail"]
    assert client.get(f"/api/v1/registries/{held}").status_code == 200

    free = create(client, auth)["slug"]
    profile = new_slug()
    assert client.put(f"/api/v1/admin/profiles/pypi/{profile}", headers=auth,
                      json={"name": "P", "registries": [{"slug": free}]}).status_code == 200

    async def state(db: AsyncSession) -> None:
        registry = await db.scalar(select(Registry).where(Registry.slug == free))
        db.add(SyncState(registry_id=registry.id, status=SyncStatus.idle))

    run_db(state)

    assert client.delete(f"/api/v1/admin/registries/{free}", headers=auth).status_code == 204
    assert client.get(f"/api/v1/registries/{free}").status_code == 404
    assert [p["registries"] for p in client.get("/api/v1/profiles", params={"ecosystem": "pypi"}).json()["data"]
            if p["slug"] == profile] == [[]]
    assert client.delete(f"/api/v1/admin/registries/{free}", headers=auth).status_code == 404


# --- profiles -----------------------------------------------------------------------------------------


def put_profile(client: TestClient, auth: dict, slug: str, members: list, **overrides: Any):
    return client.put(f"/api/v1/admin/profiles/pypi/{slug}", headers=auth,
                      json={"name": "Test profile", "registries": members, **overrides})


def test_profile_create_and_replace_keep_list_order(client: TestClient, auth: dict) -> None:
    first, second, third = (create(client, auth)["slug"] for _ in range(3))
    profile = new_slug()
    override = {"mode": "exclusive", "packages": ["torch"]}

    created = put_profile(client, auth, profile, [{"slug": second}, {"slug": first, "routing_override": override}])
    assert created.status_code == 200, created.text
    assert created.json() == {
        "slug": profile, "name": "Test profile", "ecosystem": "pypi", "is_default": False,
        "registries": [{"slug": second, "routing_override": None}, {"slug": first, "routing_override": override}],
    }

    replaced = put_profile(client, auth, profile, [{"slug": third}, {"slug": first}, {"slug": second}], name="Renamed")
    assert replaced.status_code == 200, replaced.text
    assert [m["slug"] for m in replaced.json()["registries"]] == [third, first, second]
    assert replaced.json()["name"] == "Renamed"
    assert all(m["routing_override"] is None for m in replaced.json()["registries"])

    listed = client.get("/api/v1/profiles", params={"ecosystem": "pypi"}).json()["data"]
    assert [m["slug"] for p in listed if p["slug"] == profile for m in p["registries"]] == [third, first, second]


def test_default_profile_switches(client: TestClient, auth: dict) -> None:
    member = create(client, auth)["slug"]
    one, two = new_slug(), new_slug()
    assert put_profile(client, auth, one, [{"slug": member}], is_default=True).json()["is_default"] is True
    assert put_profile(client, auth, two, [{"slug": member}], is_default=True).json()["is_default"] is True

    profiles = {p["slug"]: p for p in client.get("/api/v1/profiles", params={"ecosystem": "pypi"}).json()["data"]}
    assert profiles[one]["is_default"] is False
    assert profiles[two]["is_default"] is True
    assert sum(p["is_default"] for p in profiles.values()) == 1

    # Saving the default again without the flag leaves no default; the others are not touched.
    assert put_profile(client, auth, two, [{"slug": member}], is_default=False).json()["is_default"] is False


def test_profile_validation(client: TestClient, auth: dict) -> None:
    pypi = create(client, auth)["slug"]
    foreign = insert_foreign_registry(Ecosystem.npm, RegistryProtocol.npm_registry)
    profile = new_slug()

    for members, message in (
        ([{"slug": foreign}], "belongs to npm, not pypi"),
        ([{"slug": "b3-nope"}], "does not exist"),
        ([{"slug": pypi}, {"slug": pypi}], "listed twice"),
        ([{"slug": pypi, "routing_override": {"mode": "bogus", "packages": ["a"]}}], "unknown routing mode"),
    ):
        response = put_profile(client, auth, profile, members)
        assert response.status_code == 422, members
        assert message in response.json()["detail"], members
    assert client.put(f"/api/v1/admin/profiles/cobol/{profile}", headers=auth,
                      json={"name": "x", "registries": []}).status_code == 422
    assert profile not in [p["slug"] for p in client.get("/api/v1/profiles").json()["data"]]


def test_failed_profile_update_keeps_the_old_members(client: TestClient, auth: dict) -> None:
    member = create(client, auth)["slug"]
    profile = new_slug()
    assert put_profile(client, auth, profile, [{"slug": member}]).status_code == 200
    assert put_profile(client, auth, profile, [{"slug": member}, {"slug": "b3-nope"}]).status_code == 422
    stored = [p for p in client.get("/api/v1/profiles", params={"ecosystem": "pypi"}).json()["data"] if p["slug"] == profile]
    assert [m["slug"] for m in stored[0]["registries"]] == [member]


# --- authentication -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/admin/registries"),
        ("put", "/api/v1/admin/registries/pypi"),
        ("post", "/api/v1/admin/registries/pypi/test"),
        ("post", "/api/v1/admin/registries/pypi/enable"),
        ("post", "/api/v1/admin/registries/pypi/disable"),
        ("delete", "/api/v1/admin/registries/pypi"),
        ("put", "/api/v1/admin/profiles/pypi/default"),
        ("post", "/api/v1/admin/packages"),
    ],
)
def test_admin_routes_require_the_admin_token(client: TestClient, method: str, path: str) -> None:
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_reads_are_open(client: TestClient) -> None:
    assert client.get("/api/v1/registries").status_code == 200
    assert client.get("/api/v1/profiles").status_code == 200


# --- POST /admin/packages and the task -----------------------------------------------------------------


def test_fetch_packages_rejects_bad_purls_and_queues_nothing(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list = []
    monkeypatch.setattr(admin_api, "fetch_packages", SimpleNamespace(delay=lambda purls: queued.append(purls)))

    response = client.post("/api/v1/admin/packages", headers=auth,
                           json={"purls": ["pkg:pypi/flask", "flask", "pkg:npm/left-pad", "pkg:pypi/ns/pkg"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "flask:" in detail and "pkg:npm/left-pad" in detail and "pkg:pypi/ns/pkg" in detail
    assert "pkg:pypi/flask:" not in detail
    assert queued == []

    for purls in ([], ["pkg:pypi/x"] * 501):
        assert client.post("/api/v1/admin/packages", headers=auth, json={"purls": purls}).status_code == 422
    assert queued == []


def test_fetch_packages_queues_one_task(client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list = []

    def delay(purls: list[str]) -> SimpleNamespace:
        queued.append(purls)
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(admin_api, "fetch_packages", SimpleNamespace(delay=delay))
    purls = ["pkg:pypi/flask", "pkg:pypi/Django@5.0"]
    response = client.post("/api/v1/admin/packages", headers=auth, json={"purls": purls})
    assert response.status_code == 200
    assert response.json() == {"task_id": "task-1", "status": "queued", "operation": "fetch_packages"}
    assert queued == [purls]


def test_fetch_packages_task_counts_and_records_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ["b3-ok", "b3-missing", "b3-boom"]
    seen: list[str] = []

    async def fake_sync(db: AsyncSession, ref, *, origin):
        seen.append(f"{ref.name}:{origin.value}")
        if ref.name == "b3-missing":
            raise PackageNotFound(ref.name)
        if ref.name == "b3-boom":
            raise httpx.ReadTimeout("upstream slow")

    monkeypatch.setattr(fetch_task, "sync_package", fake_sync)
    result = asyncio.run(fetch_task._fetch_packages_async([f"pkg:pypi/{n}" for n in names]))
    assert (result["requested"], result["fetched"], result["not_found"], result["failed"]) == (3, 1, 1, 1)
    assert seen == ["b3-ok:manual", "b3-missing:manual", "b3-boom:manual"]

    failures = run_db(lambda db: _failures(db))
    assert failures == {"b3-missing": ("PackageNotFound", False), "b3-boom": ("ReadTimeout", True)}


async def _failures(db: AsyncSession) -> dict:
    rows = await db.scalars(select(SyncFailure).where(SyncFailure.package_ref.like(f"pkg:pypi/{PREFIX}%")))
    return {row.package_ref.removeprefix("pkg:pypi/"): (row.error_class, row.retryable) for row in rows}


# --- registries import ---------------------------------------------------------------------------------


def run_import(document: dict) -> list[service.ImportResult]:
    parsed = service.parse_import(yaml.safe_dump(document))
    return run_db(lambda db: service.import_document(db, parsed))


def import_document_for(slug: str, profile: str, **registry: Any) -> dict:
    return {
        "registries": [body(slug, **registry)],
        "profiles": [{"slug": profile, "name": "Imported", "ecosystem": "pypi", "registries": [{"slug": slug}]}],
    }


def test_import_creates_then_reports_unchanged_then_updates(client: TestClient) -> None:
    slug, profile = new_slug(), new_slug()
    document = import_document_for(slug, profile)

    first = run_import(document)
    assert [(r.kind, r.outcome) for r in first] == [("registry", "created"), ("profile", "created")]
    stored = client.get(f"/api/v1/registries/{slug}").json()
    assert stored["enabled"] is False and stored["priority"] == 100

    second = run_import(document)
    assert [r.outcome for r in second] == ["unchanged", "unchanged"]

    document["registries"][0]["priority"] = 9
    document["profiles"][0]["name"] = "Renamed"
    third = run_import(document)
    assert [r.outcome for r in third] == ["updated", "updated"]
    assert client.get(f"/api/v1/registries/{slug}").json()["priority"] == 9
    assert [p["name"] for p in client.get("/api/v1/profiles").json()["data"] if p["slug"] == profile] == ["Renamed"]


def test_import_enables_only_after_a_passing_test(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    slug, profile = new_slug(), new_slug()
    document = import_document_for(slug, profile, enabled=True)

    patch_connection(monkeypatch, ConnectionError("refused"))
    failed = run_import(document)
    assert failed[0].outcome == "error" and "ConnectionError: refused" in failed[0].detail
    assert failed[0].failed
    assert client.get(f"/api/v1/registries/{slug}").json()["enabled"] is False
    assert failed[1].outcome == "created"  # the profile does not depend on the registry being enabled

    patch_connection(monkeypatch)
    passed = run_import(document)
    assert (passed[0].outcome, passed[0].detail) == ("updated", "enabled")
    assert client.get(f"/api/v1/registries/{slug}").json()["enabled"] is True

    assert run_import(document)[0].outcome == "unchanged"  # already on: no second test needed

    document["registries"][0]["enabled"] = False
    off = run_import(document)
    assert (off[0].outcome, off[0].detail) == ("updated", "disabled")
    assert client.get(f"/api/v1/registries/{slug}").json()["enabled"] is False


def test_import_reports_each_bad_object_and_goes_on(client: TestClient) -> None:
    good, bad, moved = new_slug(), new_slug(), new_slug()
    run_import({"registries": [body(moved)]})
    document = {"registries": [
        body(bad, protocol="pypi-simple"),
        body(good),
        body(moved, protocol="pypi-simple"),   # protocol of an existing source cannot change
    ], "profiles": [{"slug": new_slug(), "name": "P", "ecosystem": "pypi", "registries": [{"slug": bad}]}]}

    results = run_import(document)
    assert [r.outcome for r in results] == ["error", "created", "error", "error"]
    assert "no adapter for protocol" in results[0].detail
    assert "cannot be changed" in results[2].detail
    assert "does not exist" in results[3].detail
    assert client.get(f"/api/v1/registries/{good}").status_code == 200
    assert client.get(f"/api/v1/registries/{bad}").status_code == 404
