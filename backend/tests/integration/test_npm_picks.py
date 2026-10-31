"""#15: the dependency tree and dependency links pick an npm version as resolve does (npm-pick-manifest), not the
highest match."""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import package_detail as routes
from app.config import get_settings
from app.ecosystems.base import NormalizedDependency, NormalizedPackage, NormalizedVersion, PackageRef
from app.fetcher import pipeline
from app.models.dependency import Dependency
from app.models.enums import PackageOrigin
from app.models.registry import Registry
from app.models.version import PackageVersion


def test_latest_below_the_highest_match_wins_in_the_tree_and_the_link() -> None:
    npm = pipeline.plugin_for("npm")

    async def check() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                registry = await db.scalar(select(Registry).where(Registry.slug == "npmjs"))
                lib, app_name = f"lib{uuid.uuid4().hex[:8]}", f"app{uuid.uuid4().hex[:8]}"
                # 1.1.0 is a later line, not `latest`: npm installs 1.0.0 for ^1.0.0.
                await pipeline.write_package(db, npm, [NormalizedPackage(
                    ref=PackageRef("npm", "", lib), registry_id=str(registry.id), display_name=lib, revision="1",
                    dist_tags={"latest": "1.0.0"},
                    versions=[NormalizedVersion(version="1.0.0", status="active", dependencies=[]),
                              NormalizedVersion(version="1.1.0", status="active", dependencies=[])])],
                    missing_registry_ids=(), origin=PackageOrigin.seed)
                dep = NormalizedDependency(target=PackageRef("npm", "", lib), constraint="^1.0.0",
                                           dependency_type="runtime", raw_spec=f"{lib}@^1.0.0")
                package = await pipeline.write_package(db, npm, [NormalizedPackage(
                    ref=PackageRef("npm", "", app_name), registry_id=str(registry.id), display_name=app_name,
                    revision="1", versions=[NormalizedVersion(version="1.0.0", status="active", dependencies=[dep])])],
                    missing_registry_ids=(), origin=PackageOrigin.seed)
                await db.flush()
                link = await db.scalar(select(PackageVersion.version).join(
                    Dependency, Dependency.resolved_version_id == PackageVersion.id).where(Dependency.dependency_name == lib))
                assert link == "1.0.0"
                tree = await routes.get_dependencies_tree(
                    "npm", app_name, "1.0.0", db, profile=None, depth=1, include_optional=False, python_version=None,
                    os=None, node_version=None, cpu=None, target_triple=None, features=None, default_features=None,
                    target_framework=None, scopes=None, arch=None)
                assert [(n.name, n.resolved_version) for n in tree.dependencies] == [(lib, "1.0.0")]
                assert package.name == app_name
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(check())
