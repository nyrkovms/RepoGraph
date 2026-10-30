"""crates.io end to end on the stack's database (IMPL G3, G6, G7): the crates plugin, the crates-io starter source of
migration 0009, index files and API answers from the replay fixtures."""

import asyncio
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import package_detail as routes
from app.db import loop_local_session
from app.ecosystems.base import NormalizedDependency, NormalizedPackage, NormalizedVersion, PackageRef
from app.fetcher import pipeline
from app.models.enums import PackageOrigin
from app.models.package_event import PackageEvent
from app.models.registry import Registry
from app.models.run import Run
from app.models.version import PackageVersion
from app.tasks import daily_update
from app.tasks.fetch_packages import _fetch_packages_async

SERDE = PackageRef("crates", "", "serde")


def test_a_crate_is_read_from_the_index_and_the_api_then_rechecked_by_etag() -> None:
    async def sync() -> tuple[object, list[PackageVersion]]:
        async with loop_local_session() as db:
            await pipeline.sync_package(db, SERDE, origin=PackageOrigin.manual)
            await db.commit()
            package = await pipeline.find_package(db, SERDE)
            versions = list((await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id)))
                            .all())
            return package, versions

    package, versions = asyncio.run(sync())
    assert package.name == "serde" and package.description and package.license_spdx == "MIT OR Apache-2.0"
    assert len(versions) > 300 and all(v.release_date for v in versions), "pubtime dates every version"
    latest = max(versions, key=lambda v: v.sort_key)
    assert {"default", "derive", "std"} <= set(latest.features or {})
    assert any(v.status.value == "yanked" for v in versions)

    async def run_daily() -> dict:
        async with loop_local_session() as db:
            result = await daily_update._run_daily_update_async("crates")
            await db.execute(delete(PackageEvent).where(PackageEvent.run_id == uuid.UUID(result["run_id"])))
            await db.execute(delete(Run).where(Run.id == uuid.UUID(result["run_id"])))
            await db.commit()
            return result

    result = asyncio.run(run_daily())
    # The replayed index file answers 304 to the stored ETag: nothing changed.
    assert result["status"] == "success" and result["changed"] == 0
    assert result["per_registry"]["crates-io"]["changed"] == 0


def test_a_crate_asked_for_in_another_spelling_is_stored_under_its_published_name() -> None:
    result = asyncio.run(_fetch_packages_async(["pkg:cargo/serde-json"]))
    assert (result["fetched"], result["not_found"], result["failed"]) == (1, 0, 0)

    async def names() -> tuple[object, object]:
        async with loop_local_session() as db:
            return (await pipeline.find_package(db, PackageRef("crates", "", "serde_json")),
                    await pipeline.find_package(db, PackageRef("crates", "", "serde-json")))

    published, folded = asyncio.run(names())
    assert published is not None and published.name == "serde_json" and folded is None


def _dep(name: str, kind: str = "runtime", *, target: str | None = None, enabled_by: list[str] | None = None,
         features: list[str] | None = None) -> NormalizedDependency:
    details: dict[str, object] = {}
    if enabled_by:
        details["enabled_by"] = enabled_by
    if features:
        details["features"] = features
    return NormalizedDependency(
        target=PackageRef("crates", "", name), constraint="^1", dependency_type=kind,  # type: ignore[arg-type]
        raw_spec=f'{name} = "^1"', scope="normal", environment_markers={"rust_cfg": target} if target else None,
        details=details or None)


def test_the_tree_follows_target_and_features() -> None:
    crates = pipeline.plugin_for("crates")

    async def check() -> None:
        async with loop_local_session() as db:
            try:
                await _tree_cases(db, crates)
            finally:
                await db.rollback()

    asyncio.run(check())


async def _tree_cases(db: AsyncSession, crates: object) -> None:
    crates_io = await db.scalar(select(Registry).where(Registry.slug == "crates-io"))
    suffix = uuid.uuid4().hex[:8]
    root, win, extra, shared, deep = (f"{n}-{suffix}" for n in ("root", "win", "extra", "shared", "deep"))

    async def write(name: str, dependencies: list[NormalizedDependency]) -> None:
        snapshot = NormalizedPackage(
            ref=PackageRef("crates", "", name), registry_id=str(crates_io.id), display_name=name, revision="1",
            versions=[NormalizedVersion(version="1.0.0", status="active", dependencies=dependencies)])
        await pipeline.write_package(db, crates, [snapshot], missing_registry_ids=(), origin=PackageOrigin.seed)  # type: ignore[arg-type]
        await db.flush()

    await write(deep, [])
    await write(win, [])
    await write(extra, [])
    # `shared` turns `deep` on through its feature `more`, which only `root`'s edge asks for.
    await write(shared, [_dep(deep, "optional", enabled_by=["more"])])
    await write(root, [_dep(win, target="cfg(windows)"), _dep(extra, "optional", enabled_by=["default", "extra"]),
                       _dep(shared, features=["more"])])

    async def tree(target: str | None, features: str | None = None, default_features: bool | None = None) -> list[str]:
        response = await routes.get_dependencies_tree(
            "crates", root, "1.0.0", db, profile=None, depth=3, include_optional=False, python_version=None,
            os=None, node_version=None, cpu=None, target_triple=target, features=features,
            default_features=default_features)
        out: list[str] = []
        stack = [(node, "") for node in response.dependencies]
        while stack:
            node, prefix = stack.pop(0)
            out.append(prefix + node.name.removesuffix(f"-{suffix}"))
            stack.extend((child, prefix + "  ") for child in node.children)
        return sorted(out)   # the tree lists runtime dependencies before optional ones

    assert await tree(None) == sorted(["extra", "shared", "  deep"])
    assert await tree("x86_64-pc-windows-msvc") == sorted(["extra", "shared", "win", "  deep"])
    assert await tree("x86_64-unknown-linux-gnu", default_features=False) == sorted(["shared", "  deep"])
    assert await tree(None, features="extra", default_features=False) == sorted(["extra", "shared", "  deep"])
    try:
        await tree("plan9")
    except routes.HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("an unknown target is a 422")
