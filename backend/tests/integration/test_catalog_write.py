"""The catalog write path (IMPL B1) on a real database. Every test runs in a transaction that is rolled back,
so the seeded catalog the other integration tests use stays as it is."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.base import (
    NormalizedArtifact,
    NormalizedAuthor,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    NormalizedVulnerability,
    PackageRef,
)
from app.ecosystems.pypi.adapters import WarehouseAdapter
from app.fetcher import pipeline
from app.fetcher.lazy import LazyFetcher
from app.models.artifact import Artifact
from app.models.author import PackageAuthor
from app.models.dependency import Dependency
from app.models.enums import DependenciesState, PackageEventKind, PackageOrigin, RegistryProtocol
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.version import PackageVersion, VersionSource
from app.models.vulnerability import PackageVulnerability

PLUGIN = pipeline.plugin_for("pypi")


def _in_rolled_back_transaction(test: Callable[[AsyncSession, str, str], Awaitable[None]]) -> None:
    async def run() -> None:
        # Own engine per event loop: pooled asyncpg connections must not outlive the loop of asyncio.run.
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                pypi = await pipeline.get_or_create_pypi_registry(db)
                mirror = Registry(
                    ecosystem=pypi.ecosystem, name="Test mirror", slug=f"b1-mirror-{uuid.uuid4().hex[:8]}",
                    base_url="https://mirror.invalid/simple/", protocol=RegistryProtocol.pypi_simple, priority=50,
                )
                db.add(mirror)
                await db.flush()
                await test(db, str(pypi.id), str(mirror.id))
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(run())


def _name() -> str:
    return f"b1-test-{uuid.uuid4().hex[:10]}"


def _dep(name: str, constraint: str = ">=1") -> NormalizedDependency:
    return NormalizedDependency(target=PackageRef(ecosystem="pypi", namespace="", name=name), constraint=constraint,
                                dependency_type="runtime", raw_spec=f"{name}{constraint}")


def _version(version: str, deps: list[NormalizedDependency] | None, status: str = "active",
             files: tuple[str, ...] = ()) -> NormalizedVersion:
    return NormalizedVersion(
        version=version, status=status, dependencies=deps,  # type: ignore[arg-type]
        engines={"python": ">=3.9"},
        artifacts=[NormalizedArtifact(filename=f, artifact_type="wheel", download_url=f"https://files/{f}",
                                      checksums={"sha256": "a" * 64}) for f in files],
    )


def _snapshot(name: str, registry_id: str, versions: list[NormalizedVersion], **fields) -> NormalizedPackage:
    return NormalizedPackage(ref=PackageRef(ecosystem="pypi", namespace="", name=name), registry_id=registry_id,
                             display_name=name, revision="1", versions=versions, **fields)


async def _write(db: AsyncSession, *snapshots: NormalizedPackage, missing: tuple[str, ...] = (), origin=PackageOrigin.seed):
    return await pipeline.write_package(db, PLUGIN, list(snapshots), missing_registry_ids=missing, origin=origin)


async def _versions(db: AsyncSession, package_id) -> dict[str, PackageVersion]:
    rows = (await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package_id))).all()
    return {row.version: row for row in rows}


async def _deps(db: AsyncSession, version_id, variant: str | None = None) -> list[str]:
    condition = Dependency.variant_registry_id.is_(None) if variant is None else Dependency.variant_registry_id == variant
    rows = (await db.scalars(select(Dependency).where(Dependency.version_id == version_id, condition))).all()
    return sorted(row.dependency_name for row in rows)


async def _sources(db: AsyncSession, version_id) -> dict[str, str]:
    rows = (await db.scalars(select(VersionSource).where(VersionSource.version_id == version_id))).all()
    return {str(row.registry_id): row.status.value for row in rows}


async def _events(db: AsyncSession, package_id) -> list[tuple[str, str | None]]:
    rows = (await db.scalars(select(PackageEvent).where(PackageEvent.package_id == package_id)
                             .order_by(PackageEvent.detected_at, PackageEvent.id))).all()
    return [(row.kind.value, row.version) for row in rows]


def test_one_source_writes_the_catalog_row() -> None:
    async def test(db: AsyncSession, pypi: str, _mirror: str) -> None:
        name = _name()
        package = await _write(db, _snapshot(name, pypi, [
            _version("1.0", None, files=("a-1.0.whl",)),
            _version("2.0", [_dep("typing-extensions")], files=("a-2.0.whl",)),
        ], dist_tags={"latest": "2.0"}))

        assert (package.namespace, package.origin, package.latest_version) == ("", PackageOrigin.seed, "2.0")
        versions = await _versions(db, package.id)
        assert versions["2.0"].dependencies_state == DependenciesState.hydrated
        assert versions["1.0"].dependencies_state == DependenciesState.pending   # lazy: not loaded yet
        assert versions["2.0"].requires_python == ">=3.9" and versions["2.0"].sort_key > versions["1.0"].sort_key
        assert str(versions["2.0"].metadata_registry_id) == pypi
        assert await _deps(db, versions["2.0"].id) == ["typing-extensions"]
        assert await _sources(db, versions["1.0"].id) == {pypi: "active"}
        registry = await db.scalar(select(PackageRegistry).where(PackageRegistry.package_id == package.id))
        assert (registry.is_primary, registry.upstream_revision, registry.missing_since) == (True, "1", None)
        assert await _events(db, package.id) == [("tracking_started", None)]

        # A refresh that does not carry the dependencies of 2.0 keeps them (dependencies=None = not loaded).
        await _write(db, _snapshot(name, pypi, [_version("1.0", None), _version("2.0", None)]))
        assert await _deps(db, versions["2.0"].id) == ["typing-extensions"]

    _in_rolled_back_transaction(test)


def test_two_sources_with_the_same_and_different_metadata() -> None:
    async def test(db: AsyncSession, pypi: str, mirror: str) -> None:
        name = _name()
        package = await _write(
            db,
            _snapshot(name, pypi, [_version("1.0", [_dep("filelock")], files=("t-1.0-pypi.whl",)),
                                   _version("2.0", [_dep("filelock"), _dep("nvidia-cudnn-cu12")])]),
            _snapshot(name, mirror, [_version("1.0", [_dep("filelock")], files=("t-1.0+cu.whl",)),
                                     _version("2.0", [_dep("filelock")])]),
        )
        versions = await _versions(db, package.id)

        assert await _sources(db, versions["1.0"].id) == {pypi: "active", mirror: "active"}
        assert await _deps(db, versions["1.0"].id, variant=mirror) == []                      # same metadata
        assert await _deps(db, versions["2.0"].id) == ["filelock", "nvidia-cudnn-cu12"]       # canonical: pypi.org
        assert await _deps(db, versions["2.0"].id, variant=mirror) == ["filelock"]            # variant (Q17)
        artifacts = (await db.execute(select(Artifact.filename, Artifact.registry_id)
                                      .where(Artifact.version_id == versions["1.0"].id))).all()
        assert sorted((f, str(r)) for f, r in artifacts) == sorted([("t-1.0-pypi.whl", pypi), ("t-1.0+cu.whl", mirror)])

        # The mirror catches up with pypi.org: its variant goes away.
        await _write(
            db,
            _snapshot(name, pypi, [_version("1.0", [_dep("filelock")]), _version("2.0", [_dep("filelock"), _dep("nvidia-cudnn-cu12")])]),
            _snapshot(name, mirror, [_version("1.0", [_dep("filelock")]), _version("2.0", [_dep("filelock"), _dep("nvidia-cudnn-cu12")])]),
        )
        assert await _deps(db, versions["2.0"].id, variant=mirror) == []

    _in_rolled_back_transaction(test)


def test_version_appearing_in_a_second_source_is_not_a_new_release() -> None:
    async def test(db: AsyncSession, pypi: str, mirror: str) -> None:
        name = _name()
        package = await _write(db, _snapshot(name, pypi, [_version("1.0", [])]),
                               _snapshot(name, mirror, [_version("0.9", [])]))
        await _write(db, _snapshot(name, pypi, [_version("1.0", []), _version("1.1", [])]),
                     _snapshot(name, mirror, [_version("0.9", []), _version("1.0", [])]))

        events = await _events(db, package.id)
        assert events[1:] == [("release_added", "1.1"), ("source_added", "1.0")]
        source_event = await db.scalar(select(PackageEvent).where(PackageEvent.package_id == package.id,
                                                                  PackageEvent.kind == PackageEventKind.source_added))
        assert source_event.details["registry"].startswith("b1-mirror-")

    _in_rolled_back_transaction(test)


def test_version_disappearing_from_one_source() -> None:
    async def test(db: AsyncSession, pypi: str, mirror: str) -> None:
        name = _name()
        package = await _write(db, _snapshot(name, pypi, [_version("1.0", [])]),
                               _snapshot(name, mirror, [_version("1.0", []), _version("1.0+cu", [])]))
        # 1.0+cu leaves the mirror, then the mirror loses the package altogether.
        await _write(db, _snapshot(name, pypi, [_version("1.0", [])]), _snapshot(name, mirror, [_version("1.0", [])]))
        versions = await _versions(db, package.id)
        assert versions["1.0+cu"].status.value == "removed"
        assert await _sources(db, versions["1.0+cu"].id) == {mirror: "removed"}
        assert await _sources(db, versions["1.0"].id) == {pypi: "active", mirror: "active"}

        await _write(db, _snapshot(name, pypi, [_version("1.0", [])]), missing=(mirror,))
        assert versions["1.0"].status.value == "active"
        assert await _sources(db, versions["1.0"].id) == {pypi: "active", mirror: "removed"}
        gone = await db.scalar(select(PackageRegistry).where(PackageRegistry.package_id == package.id,
                                                             PackageRegistry.registry_id == uuid.UUID(mirror)))
        assert gone.missing_since is not None
        assert ("release_removed", "1.0") not in await _events(db, package.id)

    _in_rolled_back_transaction(test)


def test_index_without_package_metadata_does_not_wipe_authors_and_vulnerabilities() -> None:
    async def test(db: AsyncSession, pypi: str, mirror: str) -> None:
        name = _name()
        author = NormalizedAuthor(name="B1 Author", email=None, url=None, role="author")
        vuln = NormalizedVulnerability(source="pypi_native", source_id=f"PYSEC-{name}", aliases=[], summary="s",
                                       details=None, severity=None, fixed_in=None, published_at=None, withdrawn_at=None,
                                       link="https://osv.dev")
        package = await _write(db, _snapshot(name, pypi, [_version("1.0", [])], description="d", authors=[author],
                                             vulnerabilities=[vuln]))
        # The pypi-simple index becomes the only answering source (pypi.org is not asked): it has files only.
        await _write(db, _snapshot(name, mirror, [_version("1.0", None)]))

        authors = (await db.scalars(select(PackageAuthor).where(PackageAuthor.package_id == package.id))).all()
        vulns = (await db.scalars(select(PackageVulnerability).where(PackageVulnerability.package_id == package.id))).all()
        assert len(authors) == 1 and len(vulns) == 1

    _in_rolled_back_transaction(test)


def test_lazy_fetch_writes_through_the_pipeline(monkeypatch) -> None:
    name = _name()

    async def fake_fetch(self, ref, lookup):
        return _snapshot(ref.name, self.registry.id, [_version("1.0", [_dep("filelock")])], description="lazy")

    monkeypatch.setattr(WarehouseAdapter, "fetch_package", fake_fetch)

    async def test(db: AsyncSession, _pypi: str, _mirror: str) -> None:
        fetcher = LazyFetcher(db=db)
        monkeypatch.setattr(db, "commit", db.flush)   # LazyFetcher commits; keep the test transaction open
        package = await fetcher.ensure_package(PackageRef(ecosystem="pypi", namespace="", name=name))

        assert package is not None and package.origin == PackageOrigin.discovered
        assert package.description == "lazy"
        versions = await _versions(db, package.id)
        assert await _deps(db, versions["1.0"].id) == ["filelock"]

    _in_rolled_back_transaction(test)
