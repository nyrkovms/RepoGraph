"""Dependencies as each source publishes them (IMPL D3, SPEC §6.5, §9.1): read lazily from the source a reader takes
the version from; different metadata becomes a variant, none makes them unknown (Q14). Rolled-back transactions,
adapters replaced by stubs."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import package_detail as routes
from app.config import get_settings
from app.ecosystems.base import (
    FetchedDocument,
    NormalizedArtifact,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    PackageRef,
    VersionDependencies,
)
from app.ecosystems.pypi.plugin import PyPIPlugin
from app.fetcher import pipeline
from app.models.dependency import Dependency
from app.models.enums import Ecosystem, PackageOrigin, RegistryProtocol
from app.models.registry import Registry
from app.models.source_document import SourceDocument
from app.models.source_profile import SourceProfile, SourceProfileRegistry
from app.models.version import VersionSource
from app.resolver.service import resolve_graph
from app.schemas.resolve import GraphResolveRequest

PLUGIN = pipeline.plugin_for("pypi")


def _dep(name: str) -> NormalizedDependency:
    return NormalizedDependency(target=PackageRef(ecosystem="pypi", namespace="", name=name), constraint="",
                                dependency_type="runtime", raw_spec=name)


class _Stub:
    """The mirror's adapter: answers fetch_version_dependencies from `answers` and counts the calls."""

    def __init__(self, registry, answers: dict[str, VersionDependencies], calls: list[str]) -> None:
        self.registry = registry
        self.answers = answers
        self.calls = calls

    async def fetch_package(self, ref, lookup):
        self.calls.append(f"fetch {ref.name}")
        versions = self.answers.get(f"package:{ref.name}")
        if versions is None:
            return None
        return NormalizedPackage(ref=ref, registry_id=self.registry.id, display_name=ref.name, revision="e1",
                                 versions=versions)

    async def fetch_version_dependencies(self, ref, version, lookup):
        self.calls.append(f"{ref.name}@{version}")
        return self.answers[ref.name]


def _run(monkeypatch, answers: dict[str, VersionDependencies],
         test: Callable[[AsyncSession, Registry, Registry, list[str]], Awaitable[None]]) -> None:
    calls: list[str] = []
    real_adapter = PyPIPlugin.adapter

    def adapter(self, registry):
        if registry.slug.startswith("d3-mirror-"):
            return _Stub(registry, answers, calls)
        if registry.slug == "pypi" and "pypi" in answers:
            return _Stub(registry, answers["pypi"], calls)   # pypi.org answers too (a re-read of a known package)
        return real_adapter(self, registry)

    monkeypatch.setattr(PyPIPlugin, "adapter", adapter)

    async def run() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                pypi = await pipeline.get_or_create_pypi_registry(db)
                mirror = Registry(ecosystem=pypi.ecosystem, name="Index", slug=f"d3-mirror-{uuid.uuid4().hex[:8]}",
                                  base_url="https://index.invalid/simple/", protocol=RegistryProtocol.pypi_simple,
                                  priority=50)
                db.add(mirror)
                await db.flush()
                await test(db, pypi, mirror, calls)
            finally:
                await db.close()
                await transaction.rollback()
        await engine.dispose()

    asyncio.run(run())


async def _write(db: AsyncSession, name: str, pypi: Registry, mirror: Registry, deps: list[str]) -> None:
    """1.0 in pypi.org with `deps` (hydrated, canonical) and in the mirror without dependencies yet (an index)."""
    ref = PackageRef(ecosystem="pypi", namespace="", name=name)
    snapshots = [
        NormalizedPackage(ref=ref, registry_id=str(pypi.id), display_name=name, revision="1",
                          versions=[NormalizedVersion(version="1.0", status="active", dependencies=[_dep(d) for d in deps])]),
        NormalizedPackage(ref=ref, registry_id=str(mirror.id), display_name=name, revision="e1",
                          versions=[NormalizedVersion(version="1.0", status="active", dependencies=None)]),
    ]
    for dep in {*deps, "filelock"}:
        await pipeline.write_package(db, PLUGIN, [NormalizedPackage(
            ref=PackageRef(ecosystem="pypi", namespace="", name=dep), registry_id=str(pypi.id), display_name=dep,
            revision="1", versions=[NormalizedVersion(version="1.0", status="active", dependencies=[])])],
            missing_registry_ids=(), origin=PackageOrigin.dependency)
    await pipeline.write_package(db, PLUGIN, snapshots, missing_registry_ids=(), origin=PackageOrigin.seed)
    await db.commit()


async def _profile(db: AsyncSession, *registries: Registry) -> str:
    """Like pip's --index-url <first> --extra-index-url <rest>."""
    slug = f"d3-{uuid.uuid4().hex[:8]}"
    profile = SourceProfile(ecosystem=Ecosystem.pypi, slug=slug, name=slug, is_default=False)
    db.add(profile)
    await db.flush()
    for position, registry in enumerate(registries):
        db.add(SourceProfileRegistry(profile_id=profile.id, registry_id=registry.id, ecosystem=Ecosystem.pypi,
                                     position=position))
    await db.commit()
    return slug


async def _graph(db: AsyncSession, name: str, profile: str | None = None) -> dict[str, list[str]]:
    graph = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=profile))
    assert graph.error is None, graph.error
    return {node.name: node.errors for node in graph.nodes}


def test_source_with_other_metadata_gets_a_variant(monkeypatch) -> None:
    name = f"d3-{uuid.uuid4().hex[:10]}"
    document = FetchedDocument(path=f"{name}/{name}-1.0-py3-none-any.whl.metadata", content=b"Requires-Dist: filelock",
                               content_type="text/plain", etag='"m1"')
    answers = {name: VersionDependencies([_dep("filelock")], documents=[document])}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, name, pypi, mirror, ["filelock", "nvidia-blob"])
        assert set(await _graph(db, name)) == {name, "filelock", "nvidia-blob"}   # pypi.org: canonical set
        assert calls == []
        through_index = await _profile(db, mirror, pypi)
        assert set(await _graph(db, name, through_index)) == {name, "filelock"}   # the index's own metadata
        assert calls == [f"{name}@1.0"]
        version_id = await db.scalar(select(VersionSource.version_id).where(VersionSource.registry_id == mirror.id))
        variant = (await db.scalars(select(Dependency.dependency_name).where(
            Dependency.version_id == version_id, Dependency.variant_registry_id == mirror.id))).all()
        assert variant == ["filelock"]
        stored = await db.scalar(select(SourceDocument.content).where(SourceDocument.registry_id == mirror.id))
        assert stored == b"Requires-Dist: filelock"
        await _graph(db, name, through_index)
        assert calls == [f"{name}@1.0"]   # read once, then from the catalog

    _run(monkeypatch, answers, check)


def test_source_with_the_same_metadata_keeps_only_its_digest(monkeypatch) -> None:
    name = f"d3-{uuid.uuid4().hex[:10]}"
    answers = {name: VersionDependencies([_dep("filelock")])}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, name, pypi, mirror, ["filelock"])
        assert set(await _graph(db, name, await _profile(db, mirror, pypi))) == {name, "filelock"}
        digest, variants = (await db.execute(
            select(VersionSource.metadata_digest, select(Dependency.id).where(
                Dependency.version_id == VersionSource.version_id, Dependency.variant_registry_id == mirror.id)
                .exists()).where(VersionSource.registry_id == mirror.id)
        )).one()
        assert digest is not None and variants is False

    _run(monkeypatch, answers, check)


def test_source_without_metadata_makes_them_unknown(monkeypatch) -> None:
    name = f"d3-{uuid.uuid4().hex[:10]}"
    answers = {name: VersionDependencies([], available=False)}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, name, pypi, mirror, ["filelock", "nvidia-blob"])
        through_index = await _profile(db, mirror, pypi)
        assert await _graph(db, name, through_index) == {name: ["dependencies unknown"]}
        assert set(await _graph(db, name)) == {name, "filelock", "nvidia-blob"}   # pypi.org still knows them

        tree = await routes.get_dependencies_tree("pypi", name, "1.0", db, profile=through_index, depth=1,
                                                  include_optional=False, python_version=None, os=None)
        assert (tree.dependencies_unknown, tree.dependencies) == (True, [])
        tree = await routes.get_dependencies_tree("pypi", name, "1.0", db, profile=None, depth=1,
                                                  include_optional=False, python_version=None, os=None)
        assert tree.dependencies_unknown is False and {n.name for n in tree.dependencies} == {"filelock", "nvidia-blob"}

    _run(monkeypatch, answers, check)


def test_source_with_no_dependencies_at_all_is_an_empty_variant(monkeypatch) -> None:
    name = f"d3-{uuid.uuid4().hex[:10]}"
    answers = {name: VersionDependencies([])}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, name, pypi, mirror, ["nvidia-blob"])
        assert await _graph(db, name, await _profile(db, mirror, pypi)) == {name: []}   # not pypi.org's nvidia-blob

    _run(monkeypatch, answers, check)


def test_source_is_read_again_when_the_canonical_set_moves(monkeypatch) -> None:
    name = f"d3-{uuid.uuid4().hex[:10]}"
    answers = {name: VersionDependencies([_dep("filelock")])}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, name, pypi, mirror, ["filelock"])                 # the index matches pypi.org: digest only
        profile = await _profile(db, mirror, pypi)
        assert set(await _graph(db, name, profile)) == {name, "filelock"}
        await _write(db, name, pypi, mirror, ["filelock", "nvidia-blob"])  # pypi.org's metadata changes
        assert set(await _graph(db, name, profile)) == {name, "filelock"}  # still the index's own set
        assert calls == [f"{name}@1.0", f"{name}@1.0"]

    _run(monkeypatch, answers, check)


def test_index_that_adds_metadata_later_is_read_again(monkeypatch) -> None:
    name = f"d3-{uuid.uuid4().hex[:10]}"
    answers = {name: VersionDependencies([], available=False)}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, name, pypi, mirror, ["filelock"])
        profile = await _profile(db, mirror, pypi)
        assert await _graph(db, name, profile) == {name: ["dependencies unknown"]}
        answers[name] = VersionDependencies([_dep("filelock")])
        snapshot = NormalizedPackage(
            ref=PackageRef(ecosystem="pypi", namespace="", name=name), registry_id=str(mirror.id), display_name=name,
            revision="e2", versions=[NormalizedVersion(version="1.0", status="active", dependencies=None, artifacts=[
                NormalizedArtifact(filename=f"{name}-1.0-py3-none-any.whl", artifact_type="wheel", download_url=None,
                                   has_metadata_file=True)])])
        await pipeline.write_package(db, PLUGIN, [snapshot], missing_registry_ids=(), origin=PackageOrigin.seed)
        await db.commit()
        assert set(await _graph(db, name, profile)) == {name, "filelock"}

    _run(monkeypatch, answers, check)


def test_tree_reads_lazily_and_survives_a_failing_source(monkeypatch) -> None:
    name, child = f"d3-{uuid.uuid4().hex[:10]}", f"d3-{uuid.uuid4().hex[:10]}"
    answers = {name: VersionDependencies([_dep(child)]), child: VersionDependencies([], available=False)}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        await _write(db, child, pypi, mirror, [])
        await _write(db, name, pypi, mirror, ["filelock"])
        profile = await _profile(db, mirror, pypi)
        tree = await routes.get_dependencies_tree("pypi", name, "1.0", db, profile=profile, depth=1,
                                                  include_optional=False, python_version=None, os=None)
        assert [n.name for n in tree.dependencies] == [child]
        assert tree.dependencies[0].dependencies_unknown is False          # not expanded, not known yet
        tree = await routes.get_dependencies_tree("pypi", name, "1.0", db, profile=profile, depth=2,
                                                  include_optional=False, python_version=None, os=None)
        assert tree.dependencies[0].dependencies_unknown is True
        tree = await routes.get_dependencies_tree("pypi", name, "1.0", db, profile=profile, depth=1,
                                                  include_optional=False, python_version=None, os=None)
        assert tree.dependencies[0].dependencies_unknown is True           # known now, without expanding it

        broken = f"d3-{uuid.uuid4().hex[:10]}"
        await _write(db, broken, pypi, mirror, ["filelock"])
        answers.pop(broken, None)                                          # the stub raises KeyError: a failing source
        tree = await routes.get_dependencies_tree("pypi", broken, "1.0", db, profile=profile, depth=1,
                                                  include_optional=False, python_version=None, os=None)
        assert (tree.dependencies_unknown, tree.dependencies) == (True, [])

    _run(monkeypatch, answers, check)


def test_package_known_from_pypi_is_linked_to_the_index_of_the_profile(monkeypatch) -> None:
    """The stage's done criterion on a catalog that already has torch from pypi.org: resolving through the index
    profile finds torch in the index, links it for everyone, picks the index build; pypi.org stays a source."""
    name = f"d3-{uuid.uuid4().hex[:10]}"
    answers = {f"package:{name}": [NormalizedVersion(version="1.0+cpu", status="active", dependencies=None)],
               name: VersionDependencies([_dep("filelock")]),
               "pypi": {f"package:{name}": [NormalizedVersion(version="1.0", status="active",
                                                              dependencies=[_dep("filelock")])]}}

    async def check(db: AsyncSession, pypi: Registry, mirror: Registry, calls: list[str]) -> None:
        ref = PackageRef(ecosystem="pypi", namespace="", name=name)
        await pipeline.write_package(db, PLUGIN, [NormalizedPackage(
            ref=ref, registry_id=str(pypi.id), display_name=name, revision="1",
            versions=[NormalizedVersion(version="1.0", status="active", dependencies=[_dep("filelock")])])],
            missing_registry_ids=(), origin=PackageOrigin.seed)
        await pipeline.write_package(db, PLUGIN, [NormalizedPackage(
            ref=PackageRef(ecosystem="pypi", namespace="", name="filelock"), registry_id=str(pypi.id),
            display_name="filelock", revision="1",
            versions=[NormalizedVersion(version="1.0", status="active", dependencies=[])])],
            missing_registry_ids=(), origin=PackageOrigin.dependency)
        await db.commit()
        exclusive = {"mode": "exclusive", "packages": [name]}
        slug = f"d3-{uuid.uuid4().hex[:8]}"
        profile = SourceProfile(ecosystem=Ecosystem.pypi, slug=slug, name=slug, is_default=False)
        db.add(profile)
        await db.flush()
        db.add_all([SourceProfileRegistry(profile_id=profile.id, registry_id=mirror.id, ecosystem=Ecosystem.pypi,
                                          position=0, routing_override=exclusive),
                    SourceProfileRegistry(profile_id=profile.id, registry_id=pypi.id, ecosystem=Ecosystem.pypi,
                                          position=1)])
        await db.commit()

        graph = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}], profile=slug))
        assert graph.error is None, graph.error
        root = next(n for n in graph.nodes if n.name == name)
        assert (root.version, root.registry) == ("1.0+cpu", mirror.slug)
        card = await routes.get_package("pypi", name, db)
        assert {s.registry for s in card.sources} == {"pypi", mirror.slug}   # visible from two sources
        default = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": f"pkg:pypi/{name}"}]))
        assert [(n.version, n.registry) for n in default.nodes if n.name == name] == [("1.0", "pypi")]

    _run(monkeypatch, answers, check)
