"""Direct coverage of ensure_version_row (version-scoped-facts, task 2.1).

Two branches, both realistic for "the version may not be in the repository":
- backfill: a bulk-synced pypi version row exists with only {files} (no info)
  -> version-scoped {info, urls} is fetched and filled;
- create: the version row is absent entirely (published after last sync /
  pruned) -> the row + artifacts are created on demand.

Hermetic against real PyPI (six is tiny/stable); deterministic regardless of
DB state left by other tests (preconditions are forced explicitly).
"""

import asyncio

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.connectors.factory import build_connector, registry_info_from_row
from app.fetcher.pipeline import (
    ensure_version_row,
    find_package,
    get_or_create_default_registry,
    run_sync_pipeline,
)
from app.models.artifact import Artifact
from app.models.classification import Classification
from app.models.dependency import Dependency
from app.models.dist_probe import DistProbe
from app.models.version import PackageVersion

PKG = "six"
HISTORICAL = "1.15.0"  # существует, не latest


def _session_factory():
    engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _sync_package(db):
    package = await find_package(db, "pypi", PKG)
    if package is None:
        registry = await get_or_create_default_registry(db, "pypi")
        connector = build_connector(registry_info_from_row(registry))
        metadata = await connector.fetch_package_metadata(PKG)
        package = await run_sync_pipeline(metadata, db, registry=registry)
    return package


async def _delete_version(db, package_id, version):
    row = await db.scalar(
        select(PackageVersion).where(
            PackageVersion.package_id == package_id, PackageVersion.version == version
        )
    )
    if row is None:
        return
    # inbound: чужие зависимости, зарезолвленные Level-1 на эту версию
    await db.execute(
        update(Dependency).where(Dependency.resolved_version_id == row.id).values(resolved_version_id=None)
    )
    # outbound children этой версии
    await db.execute(delete(Artifact).where(Artifact.version_id == row.id))
    await db.execute(delete(DistProbe).where(DistProbe.version_id == row.id))
    await db.execute(delete(Classification).where(Classification.version_id == row.id))
    await db.execute(delete(Dependency).where(Dependency.version_id == row.id))
    await db.delete(row)
    await db.commit()


def test_ensure_version_row_backfills_historical_pypi_info():
    async def scenario():
        engine, session_factory = _session_factory()
        try:
            async with session_factory() as db:
                package = await _sync_package(db)
                row = await db.scalar(
                    select(PackageVersion).where(
                        PackageVersion.package_id == package.id, PackageVersion.version == HISTORICAL
                    )
                )
                assert row is not None, "bulk sync должен создать историческую версию"
                # форсируем bulk-sync-состояние (только {files}, без info) детерминированно
                row.raw_metadata = {"files": []}
                await db.flush()

                ensured = await ensure_version_row(db, package, HISTORICAL)
                await db.commit()

                assert ensured.version == HISTORICAL
                # version-scoped info дозагружен — классификатору есть на что опереться
                assert ensured.raw_metadata.get("info"), "info исторической версии должен быть дозагружен"
                assert "summary" in ensured.raw_metadata["info"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ensure_version_row_creates_absent_version():
    async def scenario():
        engine, session_factory = _session_factory()
        try:
            async with session_factory() as db:
                package = await _sync_package(db)
                # версия отсутствует в каталоге (опубликована позже sync / удалена)
                await _delete_version(db, package.id, HISTORICAL)
                gone = await db.scalar(
                    select(PackageVersion).where(
                        PackageVersion.package_id == package.id, PackageVersion.version == HISTORICAL
                    )
                )
                assert gone is None, "версия должна отсутствовать перед созданием"

                ensured = await ensure_version_row(db, package, HISTORICAL)
                await db.commit()

                assert ensured is not None
                assert ensured.version == HISTORICAL
                assert ensured.raw_metadata.get("info")
                artifacts = (
                    await db.scalars(select(Artifact).where(Artifact.version_id == ensured.id))
                ).all()
                assert artifacts, "артефакты версии должны быть созданы"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
