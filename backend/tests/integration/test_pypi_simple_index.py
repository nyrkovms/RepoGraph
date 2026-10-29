"""A `pypi-simple` source end to end (WP D1): a registry row, `pipeline.sync_package` and
`ensure_version_dependencies` against the cpu PyTorch index page of mpmath and the PEP 658 `.metadata` of one
wheel, replayed from tests/fixtures/http (recorded from download.pytorch.org/whl/cpu/mpmath/). Runs in a
transaction that is rolled back."""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.base import PackageRef, SourceProfile
from app.fetcher import pipeline, sources
from app.models.artifact import Artifact
from app.models.dependency import Dependency
from app.models.enums import DependenciesState, PackageOrigin, RegistryProtocol
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.source_document import SourceDocument
from app.models.version import PackageVersion, VersionSource

BASE_URL = "https://download.pytorch.org/whl/cpu/"
REF = PackageRef(ecosystem="pypi", namespace="", name="mpmath")


def _in_rolled_back_transaction(test: Callable[[AsyncSession, Registry], Awaitable[None]]) -> None:
    async def run() -> None:
        # Own engine per event loop: pooled asyncpg connections must not outlive the loop of asyncio.run.
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                pypi = await pipeline.get_or_create_pypi_registry(db)
                index = Registry(
                    ecosystem=pypi.ecosystem, name="PyTorch CPU index", slug=f"d1-torch-cpu-{uuid.uuid4().hex[:8]}",
                    base_url=BASE_URL, protocol=RegistryProtocol.pypi_simple, priority=50,
                )
                db.add(index)
                await db.flush()
                await test(db, index)
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(run())


def test_pypi_simple_registry_syncs_files_and_loads_dependencies_from_pep658_metadata() -> None:
    async def test(db: AsyncSession, index: Registry) -> None:
        profile = SourceProfile(ecosystem="pypi", slug="d1", registries=[sources.registry_config(index)])
        package = await pipeline.sync_package(db, REF, origin=PackageOrigin.seed, profile=profile)

        # Only the index answered: files and versions, nothing else.
        assert (package.normalized_name, str(package.primary_registry_id)) == ("mpmath", str(index.id))
        assert package.description is None
        link = await db.scalar(select(PackageRegistry).where(PackageRegistry.package_id == package.id))
        assert link.upstream_revision and link.upstream_revision.startswith('"')   # the page ETag

        versions = {
            row.version: row
            for row in (await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id))).all()
        }
        assert {"0.1", "1.3.0", "1.4.1"} <= set(versions)
        assert all(v.dependencies_state == DependenciesState.pending for v in versions.values())
        source = await db.scalar(select(VersionSource).where(VersionSource.version_id == versions["1.4.1"].id))
        assert str(source.registry_id) == str(index.id)

        artifacts = (await db.scalars(select(Artifact).where(Artifact.version_id == versions["1.4.1"].id))).all()
        by_name = {a.filename: a for a in artifacts}
        wheel = by_name["mpmath-1.4.1-py3-none-any.whl"]
        assert wheel.has_metadata_file and wheel.artifact_type == "wheel" and len(wheel.checksum_sha256) == 64
        assert wheel.download_url.endswith("mpmath-1.4.1-py3-none-any.whl") and "#" not in wheel.download_url
        assert not by_name["mpmath-1.4.1.tar.gz"].has_metadata_file

        await pipeline.ensure_version_dependencies(db, package, versions["1.4.1"])
        version = versions["1.4.1"]
        assert version.dependencies_state == DependenciesState.hydrated and version.engines == {"python": ">=3.9"}
        rows = (await db.scalars(select(Dependency).where(Dependency.version_id == version.id))).all()
        assert rows and {row.dependency_type.value for row in rows} == {"optional"}   # mpmath has extras only
        assert {"pytest", "numpy", "gmpy2"} <= {row.dependency_name for row in rows}

        document = await db.scalar(select(SourceDocument).where(SourceDocument.registry_id == index.id))
        assert document.path.endswith("mpmath-1.4.1-py3-none-any.whl.metadata")
        assert document.content_type == "text/plain" and document.content.startswith(b"Metadata-Version:")
        assert document.sha256 == hashlib.sha256(document.content).hexdigest()

        # No file of 0.1 has a PEP 658 document: its dependencies are unknown, not empty.
        await pipeline.ensure_version_dependencies(db, package, versions["0.1"])
        assert versions["0.1"].dependencies_state == DependenciesState.unavailable

    _in_rolled_back_transaction(test)
