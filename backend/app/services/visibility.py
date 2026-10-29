"""Which versions a reader sees (SPEC-MULTI-ECOSYSTEM.md §9.1 "Видимость версий", §6.4 freeze).

A version is visible when a source the profile routes the package to publishes it; its status is the best one among
those sources. A disabled source is not in any profile, so versions only it knows (frozen data) are hidden, and a
`priority` source that has the package hides every other source, as in discovery.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ecosystems.base import EcosystemPlugin, PackageRef, RegistryConfig, SourceProfile
from app.ecosystems.routing import is_priority
from app.models.enums import PackageVersionStatus
from app.models.package_registry import PackageRegistry
from app.models.version import PackageVersion, VersionSource

# Best first: the effective status of a version seen through several sources.
_STATUS_ORDER = (
    PackageVersionStatus.active,
    PackageVersionStatus.deprecated,
    PackageVersionStatus.yanked,
    PackageVersionStatus.unlisted,
    PackageVersionStatus.superseded,
    PackageVersionStatus.removed,
)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    registry: str               # slug
    status: PackageVersionStatus


async def visible_registries(
    db: AsyncSession, plugin: EcosystemPlugin, ref: PackageRef, package_id: object, profile: SourceProfile
) -> list[RegistryConfig]:
    routed = list(plugin.sources_for(ref, profile))
    priority = [r for r in routed if is_priority(r)]
    if priority:
        present = set(
            (
                await db.scalars(
                    select(PackageRegistry.registry_id).where(
                        PackageRegistry.package_id == package_id,
                        PackageRegistry.registry_id.in_([uuid.UUID(r.id) for r in priority]),
                        PackageRegistry.missing_since.is_(None),
                    )
                )
            ).all()
        )
        for registry in priority:
            if uuid.UUID(registry.id) in present:
                return [registry]
    return routed


def visible_clause(registries: Sequence[RegistryConfig]) -> ColumnElement[bool]:
    """For a query over PackageVersion: published by one of `registries`."""
    return exists(
        select(VersionSource.version_id).where(
            VersionSource.version_id == PackageVersion.id,
            VersionSource.registry_id.in_([uuid.UUID(r.id) for r in registries]),
        )
    )


async def source_statuses(
    db: AsyncSession, registries: Sequence[RegistryConfig], version_ids: Iterable[object]
) -> dict[object, list[SourceStatus]]:
    """Per version, its status in each of `registries` that publishes it, in the order of `registries`."""
    ids = list(version_ids)
    if not ids or not registries:
        return {}
    position = {uuid.UUID(r.id): i for i, r in enumerate(registries)}
    slugs = {uuid.UUID(r.id): r.slug for r in registries}
    rows = (
        await db.execute(
            select(VersionSource.version_id, VersionSource.registry_id, VersionSource.status).where(
                VersionSource.version_id.in_(ids), VersionSource.registry_id.in_(list(position))
            )
        )
    ).all()
    result: dict[object, list[SourceStatus]] = {}
    for version_id, registry_id, status in sorted(rows, key=lambda row: position[row.registry_id]):
        result.setdefault(version_id, []).append(SourceStatus(slugs[registry_id], status))
    return result


def effective_status(statuses: Sequence[SourceStatus]) -> PackageVersionStatus | None:
    found = {s.status for s in statuses}
    return next((status for status in _STATUS_ORDER if status in found), None)


def chosen_source(statuses: Sequence[SourceStatus]) -> str | None:
    """The source a reader takes the version from: the first visible one where it has its effective status (its
    metadata, dependency variant and files are used). Tree and resolve agree on it."""
    best = effective_status(statuses)
    return next((s.registry for s in statuses if s.status == best), None)
