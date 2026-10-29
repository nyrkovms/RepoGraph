"""CatalogAccess over the database for resolvers (SPEC-MULTI-ECOSYSTEM.md §9.1): versions visible through a profile,
dependencies as a source publishes them, lazy fetch. It also records which packages the resolve read, for the
catalog revision in the answer and the cache check."""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import PackageNotFound
from app.ecosystems.base import (
    CatalogVersion,
    DependencyType,
    EcosystemPlugin,
    NormalizedDependency,
    PackageRef,
    SourceProfile,
    VersionStatus,
)
from app.fetcher.pipeline import find_package, source_dependencies, sync_package
from app.models.artifact import Artifact
from app.models.dependency import Dependency
from app.models.enums import PackageOrigin
from app.models.version import PackageVersion
from app.services import visibility

logger = logging.getLogger(__name__)


class DbCatalog:
    def __init__(self, db: AsyncSession, plugin: EcosystemPlugin) -> None:
        self.db = db
        self.plugin = plugin
        self.synced_at: dict[uuid.UUID, datetime] = {}   # package id -> synced_at when read
        self.failed = False   # a load failed and was reported as "dependencies unknown": the answer is not cached

    @property
    def revision(self) -> datetime | None:
        """The catalog revision of the answer: the latest synced_at of the packages it read."""
        return max(self.synced_at.values(), default=None)

    async def versions(self, ref: PackageRef, profile: SourceProfile) -> Sequence[CatalogVersion]:
        package = await find_package(self.db, ref)
        if package is None:
            return []
        self.synced_at[package.id] = package.synced_at
        registries = await visibility.visible_registries(self.db, self.plugin, ref, package.id, profile)
        rows = (
            await self.db.execute(
                select(PackageVersion.id, PackageVersion.version, PackageVersion.engines)
                .where(PackageVersion.package_id == package.id, visibility.visible_clause(registries))
            )
        ).all()
        statuses = await visibility.source_statuses(self.db, registries, [row.id for row in rows])
        by_slug = {r.slug: r for r in registries}
        chosen = {version_id: by_slug[slug] for version_id, found in statuses.items()
                  if (slug := visibility.chosen_source(found)) is not None}
        files: dict[tuple[uuid.UUID, str], list[str]] = {}
        if rows:
            for version_id, registry_id, filename in (
                await self.db.execute(
                    select(Artifact.version_id, Artifact.registry_id, Artifact.filename)
                    .where(Artifact.version_id.in_([row.id for row in rows]))
                )
            ).all():
                files.setdefault((version_id, str(registry_id)), []).append(filename)

        result = []
        for row in rows:
            registry = chosen.get(row.id)
            status = visibility.effective_status(statuses.get(row.id, []))
            if registry is None or status is None:
                continue
            result.append(
                CatalogVersion(
                    version=row.version,
                    registry_id=registry.id,
                    registry_slug=registry.slug,
                    status=cast(VersionStatus, status.value),
                    engines={k: str(v) for k, v in (row.engines or {}).items()},
                    artifacts=files.get((row.id, registry.id), []),
                )
            )
        return result

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        package = await find_package(self.db, ref)
        version_row = await self.db.scalar(
            select(PackageVersion).where(PackageVersion.package_id == package.id, PackageVersion.version == version)
        ) if package else None
        if package is None or version_row is None:
            return None
        try:
            rows, changed = await source_dependencies(self.db, package, version_row, registry_id)
        except Exception:  # noqa: BLE001 - a partial graph with "dependencies unknown" beats failing the resolve
            logger.warning("Dependencies of %s %s could not be loaded", ref.name, version, exc_info=True)
            self.failed = True
            await self.db.rollback()
            return None
        deps = None if rows is None else [_normalized(d) for d in rows]
        if changed:
            await self.db.commit()   # lazily loaded dependencies stay in the catalog whatever the resolve ends with
        return deps

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        """Lazy fetch of a package the catalog does not have, origin=discovered, committed at once so a later failure
        of the resolve keeps it. A catalog package the profile does not see is not fetched again: finding it in more
        sources is the presence probe's job (SPEC §6.6), and a re-read per resolve would only move its revision."""
        if await find_package(self.db, ref) is not None:
            return False
        try:
            await sync_package(self.db, ref, origin=PackageOrigin.discovered, profile=profile)
        except PackageNotFound:
            await self.db.rollback()
            return False
        except BaseException:
            await self.db.rollback()
            raise
        await self.db.commit()
        return True


def _normalized(dep: Dependency) -> NormalizedDependency:
    return NormalizedDependency(
        target=PackageRef(ecosystem=dep.dependency_ecosystem.value, namespace=dep.dependency_namespace or "",
                          name=dep.dependency_name),
        constraint=dep.version_constraint,
        dependency_type=cast(DependencyType, dep.dependency_type.value),
        raw_spec=dep.raw_spec,
        extra_name=dep.extra_name,
        environment_markers=dep.environment_markers,
    )
