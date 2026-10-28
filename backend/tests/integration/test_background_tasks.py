"""Seed, retry and the admin routes by purl, and the runs journal (IMPL B4). Tasks run against the shared database
with `sync_package` replaced, so nothing talks to the network; everything created here is removed afterwards."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import admin as admin_api
from app.config import get_settings
from app.connectors.base import PackageNotFound
from app.ecosystems.base import PackageRef
from app.models.enums import Ecosystem
from app.models.run import Run
from app.models.sync_failure import SyncFailure
from app.services import runs
from app.tasks import fetch_deps, retry_failed, seed

PREFIX = "b4-"


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


@pytest.fixture
def since():
    started = datetime.now(UTC) - timedelta(seconds=1)

    async def cleanup(db: AsyncSession) -> None:
        await db.execute(delete(Run).where(Run.operation.in_(["seed", "retry", "presence_probe", "b4-test"]),
                                           Run.started_at >= started))
        await db.execute(delete(SyncFailure).where(SyncFailure.package_name.like(f"{PREFIX}%")))

    yield started
    run_db(cleanup)


def _name(label: str) -> str:
    return f"{PREFIX}{label}-{uuid.uuid4().hex[:8]}"


def _runs(since: datetime, operation: str) -> list[Run]:
    return run_db(_select_runs(since, operation))


def _select_runs(since: datetime, operation: str):
    async def work(db: AsyncSession) -> list[Run]:
        return list((await db.scalars(select(Run).where(Run.operation == operation, Run.started_at >= since))).all())

    return work


def _failures(prefix: str = PREFIX) -> dict[str, SyncFailure]:
    async def work(db: AsyncSession) -> dict[str, SyncFailure]:
        rows = await db.scalars(select(SyncFailure).where(SyncFailure.package_name.like(f"{prefix}%")))
        return {row.package_name: row for row in rows}

    return run_db(work)


# --- seed ----------------------------------------------------------------------------------------------


def test_seed_takes_purls_and_names_and_writes_one_run(monkeypatch: pytest.MonkeyPatch, since: datetime) -> None:
    ok, missing, boom, by_name = _name("ok"), _name("missing"), _name("boom"), _name("named")
    seen: list[tuple[PackageRef, str, object]] = []

    async def fake_sync(db, ref, *, origin, run_id=None, **kwargs):
        seen.append((ref, origin.value, run_id))
        if ref.name == missing:
            raise PackageNotFound(ref.name)
        if ref.name == boom:
            raise httpx.ReadTimeout("slow")
        return SimpleNamespace(id=uuid.uuid4())

    async def no_dependencies(db, package_id):
        return []

    monkeypatch.setattr(seed, "sync_package", fake_sync)
    monkeypatch.setattr(seed, "_unresolved_dependency_refs", no_dependencies)

    result = asyncio.run(seed._run_seed_async([f"pkg:pypi/{ok}", f"pkg:pypi/{missing}", boom, by_name.upper(), "pkg:cobol/x"]))

    assert (result["processed"], result["failed"]) == (2, 3)       # the bad purl counts as failed
    assert [(ref.name, origin) for ref, origin, _ in seen] == [
        (ok, "seed"), (missing, "seed"), (boom, "seed"), (by_name, "seed")]

    (run,) = _runs(since, "seed")
    assert (run.ecosystem, run.status, run.packages_total, run.packages_updated, run.packages_failed) == (
        None, runs.PARTIAL, 5, 2, 3)
    assert run.finished_at is not None and {run_id for *_, run_id in seen} == {run.id}

    failures = _failures()
    assert set(failures) == {missing, boom}
    gone = failures[missing]
    assert (gone.operation, gone.ecosystem, gone.package_ref, gone.retryable) == (
        "seed", Ecosystem.pypi, f"pkg:pypi/{missing}", False)
    assert failures[boom].package_ref == f"pkg:pypi/{boom}" and failures[boom].retryable is True


def test_seed_follows_dependencies_of_any_ecosystem_once(monkeypatch: pytest.MonkeyPatch, since: datetime) -> None:
    root, dep = _name("root"), _name("dep")
    calls: list[tuple[str, str]] = []

    async def fake_sync(db, ref, *, origin, run_id=None, **kwargs):
        calls.append((ref.name, origin.value))
        return SimpleNamespace(id=ref.name)

    async def dependencies(db, package_id):
        return [PackageRef("pypi", "", dep)] if package_id == root else [PackageRef("pypi", "", root)]

    monkeypatch.setattr(seed, "sync_package", fake_sync)
    monkeypatch.setattr(seed, "_unresolved_dependency_refs", dependencies)

    asyncio.run(seed._run_seed_async([root]))

    assert calls == [(root, "seed"), (dep, "dependency")]       # the cycle back to root is not followed
    assert [r.status for r in _runs(since, "seed")] == [runs.SUCCESS]


# --- retry ---------------------------------------------------------------------------------------------


def test_retry_uses_package_ref_and_the_origin_of_the_operation(monkeypatch: pytest.MonkeyPatch, since: datetime) -> None:
    by_purl, legacy, daily, missing, bad_ref, probe = (_name(n) for n in ("purl", "legacy", "daily", "missing", "badref", "probe"))
    seen: dict[str, tuple[PackageRef, str]] = {}

    async def fake_sync(db, ref, *, origin, run_id=None, **kwargs):
        seen[ref.name] = (ref, origin.value)
        if ref.name == missing:
            raise PackageNotFound(ref.name)
        return SimpleNamespace(id=uuid.uuid4())

    async def due_mine(db, limit):
        return list((await db.scalars(select(SyncFailure).where(SyncFailure.package_name.like(f"{PREFIX}%"))
                                      .where(SyncFailure.operation.not_in(["presence_probe"])))).all())

    def failure(name: str, operation: str, package_ref: str | None, **extra) -> SyncFailure:
        return SyncFailure(operation=operation, package_name=name, package_ref=package_ref, error_class="ReadTimeout",
                           error_message="slow", retryable=True, **extra)

    async def setup(db: AsyncSession) -> None:
        db.add_all([
            failure(by_purl, "seed", f"pkg:pypi/{by_purl}", ecosystem=Ecosystem.pypi),
            failure(legacy, "fetch", None),                                  # written before package_ref existed
            failure(daily, "daily_update", f"pkg:pypi/{daily}", ecosystem=Ecosystem.pypi),
            failure(missing, "seed", f"pkg:pypi/{missing}", ecosystem=Ecosystem.pypi),
            failure(bad_ref, "seed", "pkg:cobol/whatever"),
        ])

    run_db(setup)
    monkeypatch.setattr(retry_failed, "sync_package", fake_sync)
    monkeypatch.setattr(retry_failed, "list_due_sync_failures", due_mine)

    result = asyncio.run(retry_failed._run_retry_failed_async(limit=50))

    assert (result["retried"], result["recovered"], result["failed"]) == (5, 3, 2)
    assert {name: (ref.ecosystem, origin) for name, (ref, origin) in seen.items()} == {
        by_purl: ("pypi", "seed"), legacy: ("pypi", "manual"), daily: ("pypi", "dependency"), missing: ("pypi", "seed")}

    (run,) = _runs(since, "retry")
    assert (run.ecosystem, run.status, run.packages_total, run.packages_updated, run.packages_failed) == (
        None, runs.PARTIAL, 5, 3, 2)

    left = _failures()
    assert set(left) == {missing, bad_ref}                                    # recovered ones are cleared
    assert left[missing].retryable is False and left[missing].attempts == 2
    assert left[bad_ref].retryable is False                                   # an unreadable purl is parked


def test_retry_leaves_probe_failures_to_the_probe(since: datetime) -> None:
    from app.services.sync_failures import list_due_sync_failures

    name = _name("probe")

    async def work(db: AsyncSession) -> list[str]:
        db.add(SyncFailure(operation="presence_probe", package_name=name, package_ref=f"pkg:pypi/{name}",
                           ecosystem=Ecosystem.pypi, error_class="ReadTimeout", error_message="slow", retryable=True))
        await db.flush()
        return [row.package_name for row in await list_due_sync_failures(db, 10_000)]

    assert name not in run_db(work)


def test_retry_with_nothing_due_writes_no_run(monkeypatch: pytest.MonkeyPatch, since: datetime) -> None:
    async def nothing(db, limit):
        return []

    monkeypatch.setattr(retry_failed, "list_due_sync_failures", nothing)
    assert asyncio.run(retry_failed._run_retry_failed_async())["retried"] == 0
    assert _runs(since, "retry") == []


# --- fetch ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["{name}", "pkg:pypi/{name}", "{upper}"])
def test_fetch_takes_a_name_or_a_purl(monkeypatch: pytest.MonkeyPatch, since: datetime, target: str) -> None:
    name = _name("fetch")
    seen: list[PackageRef] = []

    async def fake_sync(db, ref, *, origin, **kwargs):
        seen.append(ref)
        raise PackageNotFound(ref.name)

    monkeypatch.setattr(fetch_deps, "sync_package", fake_sync)
    with pytest.raises(PackageNotFound):
        asyncio.run(fetch_deps._fetch_package_async(target.format(name=name, upper=name.upper())))

    assert seen == [PackageRef("pypi", "", name)]
    failure = _failures()[name]
    assert (failure.operation, failure.package_ref, failure.ecosystem) == ("fetch", f"pkg:pypi/{name}", Ecosystem.pypi)


# --- admin routes --------------------------------------------------------------------------------------


@pytest.fixture
def auth(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


def _capture(monkeypatch: pytest.MonkeyPatch, attribute: str) -> list[tuple]:
    queued: list[tuple] = []

    def delay(*args: object) -> SimpleNamespace:
        queued.append(args)
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(admin_api, attribute, SimpleNamespace(delay=delay))
    return queued


@pytest.mark.parametrize("target", ["flask", "pkg:pypi/flask", "pkg:pypi/flask@3.0.0"])
def test_admin_fetch_queues_a_name_or_a_purl(client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    queued = _capture(monkeypatch, "fetch_missing_dependencies")
    response = client.post(f"/api/v1/admin/fetch/{target}", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"task_id": "task-1", "status": "queued", "operation": "fetch"}
    assert queued == [(target,)]


@pytest.mark.parametrize("target", ["pkg:pypi/ns/flask", "pkg:cobol/flask", "pkg:"])
def test_admin_fetch_rejects_bad_purls(client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    queued = _capture(monkeypatch, "fetch_missing_dependencies")
    assert client.post(f"/api/v1/admin/fetch/{target}", headers=auth).status_code == 422
    assert queued == []


def test_admin_fetch_requires_admin(client: TestClient) -> None:
    assert client.post("/api/v1/admin/fetch/pkg:pypi/flask").status_code == 401


def test_admin_presence_probe_queues_the_task(client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    queued = _capture(monkeypatch, "run_presence_probe")
    response = client.post("/api/v1/admin/presence-probe", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"task_id": "task-1", "status": "queued", "operation": "presence_probe"}
    # npm has no plugin yet (E1): the task would fail before journaling anything.
    assert client.post("/api/v1/admin/presence-probe", headers=auth, json={"ecosystem": "npm"}).status_code == 422
    assert client.post("/api/v1/admin/presence-probe", headers=auth, json={"ecosystem": "cobol"}).status_code == 422
    assert client.post("/api/v1/admin/presence-probe").status_code == 401
    assert queued == [("pypi",)]


def test_admin_refresh_is_gone(client: TestClient, auth: dict) -> None:
    assert client.post("/api/v1/admin/refresh", headers=auth, json={"limit": 5}).status_code in (404, 405)


def test_sync_status_lists_the_last_twenty_runs(client: TestClient, auth: dict, since: datetime) -> None:
    base = datetime.now(UTC) + timedelta(hours=1)        # newer than anything else in the journal

    async def add(db: AsyncSession) -> None:
        for n in range(22):
            db.add(Run(operation="b4-test", ecosystem=Ecosystem.pypi if n % 2 else None, status=runs.SUCCESS,
                       started_at=base + timedelta(minutes=n), finished_at=base + timedelta(minutes=n, seconds=5),
                       packages_total=n, packages_updated=1, events_created=2, error="e" if n == 21 else None))

    run_db(add)
    response = client.get("/api/v1/admin/sync/status", headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert {"status", "sync_type", "packages_synced", "packages_total", "cursor_data", "last_update_run"} <= payload.keys()

    recent = payload["recent_runs"]
    assert len(recent) == 20
    assert [r["packages_total"] for r in recent] == list(range(21, 1, -1))        # newest first
    assert recent[0]["operation"] == "b4-test" and recent[0]["error"] == "e" and recent[0]["ecosystem"] == "pypi"
    assert recent[1]["ecosystem"] is None
    assert set(recent[0]) == {
        "operation", "ecosystem", "status", "started_at", "finished_at", "packages_total", "packages_checked",
        "packages_changed", "packages_updated", "packages_failed", "packages_unavailable", "events_created", "error"}
