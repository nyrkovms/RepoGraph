"""The single write path into the catalog (SPEC-MULTI-ECOSYSTEM.md §6.5, §8.3; IMPL B1).

Seed, daily update, retries and lazy fetch call `sync_package`: fetch the package from every source it lives in,
`plugin.merge`, then upsert the package, its per-source state, versions, dependency variants, artifacts and events.
Callers commit; nothing here does.
"""

import hashlib
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import PackageNotFound
from app.ecosystems.base import (
    EcosystemPlugin,
    FetchedDocument,
    InvalidVersion,
    MergedPackage,
    MergedVersion,
    NormalizedArtifact,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    PackageRef,
    RegistryConfig,
    SourceAdapter,
    SourceProfile,
)
from app.ecosystems.merge import dependencies_digest
from app.ecosystems.registry import get_plugin, load_builtin_plugins
from app.ecosystems.routing import is_priority
from app.ecosystems.versions_common import fallback_sort_key
from app.fetcher import discovery, sources
from app.fetcher.events import EventDraft, PackageSnapshot, diff_package
from app.models.artifact import Artifact, DistTag
from app.models.author import Author, PackageAuthor
from app.models.dependency import Dependency
from app.models.enums import (
    DependenciesState,
    DependencyType,
    Ecosystem,
    PackageEventKind,
    PackageOrigin,
    PackageVersionStatus,
    RegistryProtocol,
    SyncStatus,
    VulnerabilitySeverity,
    VulnerabilitySource,
)
from app.models.package import Package
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.source_document import SourceDocument
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.models.version import VersionSource
from app.models.vulnerability import PackageVulnerability, Vulnerability

logger = logging.getLogger(__name__)


class _ProfileLookup:
    """SourceLookup over a fixed list of registries (parent POMs and BOMs from other sources of the profile)."""

    def __init__(self, plugin: EcosystemPlugin, registries: Sequence[RegistryConfig]) -> None:
        self._adapters = [plugin.adapter(registry) for registry in registries]

    def adapters_for(self, ref: PackageRef) -> Sequence[SourceAdapter]:
        return self._adapters


def pypi_ref(name: str) -> PackageRef:
    """Bare names in seed lists, CLI and sync_failures are PyPI names (IMPL T9) until B4 moves them to purl."""
    return plugin_for("pypi").coordinates.parse(name)


def plugin_for(ecosystem: str) -> EcosystemPlugin:
    load_builtin_plugins()
    return get_plugin(ecosystem)


async def sync_package(
    db: AsyncSession,
    ref: PackageRef,
    *,
    origin: PackageOrigin = PackageOrigin.dependency,
    run_id=None,
    profile: SourceProfile | None = None,
    extra_registries: Sequence[RegistryConfig] = (),
) -> Package:
    """Fetch `ref` from all its sources and write the merged result. Raises PackageNotFound if no source has it.

    A known package is re-read from the enabled registries where it lives (package_registries) that its routing
    still allows; a new one from the sources of `profile` (the ecosystem's default profile when not given).
    `extra_registries` are asked on top for a known package: sources where `presence_probe` found it.
    Sources not asked are frozen: what only they publish stays as it was (SPEC §6.4). A source error, or a
    degraded source of a known package, propagates: the package is left as it was and the caller records a sync
    failure, so a flaky source never looks like a removal.
    """
    plugin = plugin_for(ref.ecosystem)
    existing = await find_package(db, ref)
    fetched: discovery.Fetched | None = None
    if existing is not None:
        registries, frozen = await _known_sources(db, plugin, ref, existing.id, extra_registries)
        if registries:
            fetched = await discovery.fetch_known(db, plugin, ref, registries, _ProfileLookup(plugin, registries))
            if not fetched.snapshots and frozen:
                # Gone from the sources asked, still listed by a frozen one: not unavailable, nothing else changes.
                await _mark_registries_missing(db, existing.id, fetched.missing)
                return existing
        elif frozen:
            return existing   # only frozen sources list it: nothing to ask
    if fetched is None:
        profile = profile or await sources.default_profile(db, ref.ecosystem)
        registries = list(plugin.sources_for(ref, profile))
        if not registries and ref.ecosystem == "pypi" and await db.scalar(select(Registry.id).where(Registry.slug == "pypi")) is None:
            registries = [sources.registry_config(await get_or_create_pypi_registry(db))]
        if not registries:
            raise PackageNotFound(f"no enabled source for {plugin.coordinates.display(ref)}")
        fetched = await discovery.discover(
            db, plugin, ref, registries, _ProfileLookup(plugin, registries),
            use_negative_cache=origin not in (PackageOrigin.seed, PackageOrigin.manual),
        )

    if not fetched.snapshots:
        raise PackageNotFound(plugin.coordinates.display(ref))
    wrong = next((s.ref for s in fetched.snapshots if s.ref != ref), None)
    if wrong is not None:
        # A source answered for another package (an URL that meant something else upstream): writing it under
        # `ref` would replace that package's data, or empty it.
        raise PackageNotFound(f"{plugin.coordinates.display(ref)}: the source answered for {plugin.coordinates.display(wrong)}")
    return await write_package(db, plugin, fetched.snapshots, missing_registry_ids=fetched.missing, origin=origin, run_id=run_id)


async def _known_sources(
    db: AsyncSession, plugin: EcosystemPlugin, ref: PackageRef, package_id, extra: Sequence[RegistryConfig]
) -> tuple[list[RegistryConfig], bool]:
    """Sources to re-read a catalog package from, and whether a source left out still lists it (it is frozen).

    Enabled sources it is linked to that its routing allows, plus `extra`. A `priority` source that has the package
    is its only source (SPEC §6.2): the others are not asked, so a same-named package published elsewhere later
    never gets merged in.
    """
    rows = (
        await db.execute(
            select(Registry, PackageRegistry.missing_since)
            .join(PackageRegistry, PackageRegistry.registry_id == Registry.id)
            .where(PackageRegistry.package_id == package_id)
            .order_by(Registry.priority, Registry.slug)
        )
    ).all()
    enabled = [(sources.registry_config(row), missing is None) for row, missing in rows if row.enabled and row.protocol]
    allowed = {r.id for r in plugin.sources_for(ref, SourceProfile(ecosystem=ref.ecosystem, slug="catalog",
                                                                   registries=[c for c, _ in enabled]))}
    present_priority = [c for c, active in enabled if active and c.id in allowed and is_priority(c)]
    if present_priority:
        ask = present_priority[:1]
    else:
        ask = [c for c, _ in enabled if c.id in allowed]
        ask = sources.by_priority([*ask, *(r for r in extra if r.id not in {c.id for c in ask})])
    asked = {c.id for c in ask}
    frozen = any(missing is None and str(row.id) not in asked for row, missing in rows)
    return ask, frozen


async def _mark_registries_missing(db: AsyncSession, package_id, registry_ids: Sequence[str]) -> None:
    if registry_ids:
        now = datetime.now(UTC)
        await db.execute(
            update(PackageRegistry)
            .where(PackageRegistry.package_id == package_id, PackageRegistry.registry_id.in_(list(registry_ids)))
            .values(checked_at=now, missing_since=func.coalesce(PackageRegistry.missing_since, now))
        )
        await db.flush()


async def write_package(
    db: AsyncSession,
    plugin: EcosystemPlugin,
    snapshots: Sequence[NormalizedPackage],
    *,
    missing_registry_ids: Sequence[str] = (),
    origin: PackageOrigin = PackageOrigin.dependency,
    run_id=None,
) -> Package:
    """Merge snapshots of the sources (any order) and write them. `missing_registry_ids` were asked and do not
    have the package (404), as opposed to sources that were not asked at all."""
    now = datetime.now(UTC)
    registry_rows = {
        str(row.id): row
        for row in (await db.scalars(select(Registry).where(Registry.id.in_([s.registry_id for s in snapshots] + list(missing_registry_ids))))).all()
    }
    ordered = sorted(snapshots, key=lambda s: (registry_rows[s.registry_id].priority, registry_rows[s.registry_id].slug))
    merged = plugin.merge(ordered)
    ref = merged.package.ref
    by_registry = {s.registry_id: s for s in ordered}

    existing = await find_package(db, ref)
    snapshot = await _load_snapshot(db, existing) if existing else None
    incoming_versions = {v.version.version for v in merged.versions}
    frozen = await _frozen_versions(db, existing.id, registry_rows.keys(), incoming_versions) if existing else set()
    latest = _latest_version(plugin, merged)
    if existing is not None and existing.latest_version in frozen:
        latest = existing.latest_version   # a frozen source's newest version stays the latest until profiles (E0-C)

    package = await _upsert_package(db, plugin, merged, latest, origin, now, existing)
    await _upsert_package_registries(db, package, merged, missing_registry_ids, now)

    source_versions = {registry_id: {v.version: v for v in source.versions} for registry_id, source in by_registry.items()}
    version_rows, metadata_moved = await _upsert_versions(db, plugin, package.id, merged.versions)
    version_ids = {version: row.id for version, row in version_rows.items()}
    await _write_version_sources(db, package.id, version_ids, merged.versions, source_versions, registry_rows.keys(), now)
    await _mark_missing_versions_removed(db, package.id, [*version_ids.keys(), *frozen])
    for merged_version in merged.versions:
        row = version_rows[merged_version.version.version]
        await _write_dependencies(db, row, merged_version, source_versions, merged_version.version.version in metadata_moved)
    for registry_id, versions in source_versions.items():
        await _replace_artifacts(db, registry_id, {version_ids[v]: sv.artifacts for v, sv in versions.items()})

    if merged.package.dist_tags is not None:
        await _replace_dist_tags(db, package.id, merged.package.dist_tags, now)
    if merged.package.authors is not None:
        await _upsert_authors(db, package.id, merged.package.authors)
    if merged.package.vulnerabilities is not None:
        await _upsert_vulnerabilities(db, package.id, merged.package.vulnerabilities)

    await resolve_dependency_links(db, version_ids.values())
    if snapshot is None:
        await _link_dependents(db, package)

    slugs = {registry_id: row.slug for registry_id, row in registry_rows.items()}
    await _record_events(db, package.id, diff_package(snapshot, merged, plugin.versions, latest, slugs, frozen), run_id, now)
    for registry_id in [*by_registry, *missing_registry_ids]:
        await _upsert_sync_state(db, registry_id, now)

    await db.flush()
    return package


async def find_package(db: AsyncSession, ref: PackageRef) -> Package | None:
    return await db.scalar(
        select(Package).where(
            Package.ecosystem == Ecosystem(ref.ecosystem),
            func.coalesce(Package.namespace, "") == ref.namespace,
            Package.normalized_name == ref.name,
        )
    )


async def mark_unavailable(db: AsyncSession, package: Package, run_id=None) -> bool:
    """The package is gone from every source (`project_unavailable`). Returns False if it already was."""
    if package.unavailable_at is not None:
        return False
    now = datetime.now(UTC)
    package.unavailable_at = now
    await db.execute(
        update(PackageRegistry)
        .where(PackageRegistry.package_id == package.id, PackageRegistry.missing_since.is_(None))
        .values(missing_since=now, checked_at=now)
    )
    db.add(PackageEvent(package_id=package.id, kind=PackageEventKind.project_unavailable, detected_at=now, run_id=run_id))
    return True


# --- reading the stored state ---------------------------------------------------------------------


async def _load_snapshot(db: AsyncSession, package: Package) -> PackageSnapshot:
    version_rows = (
        await db.execute(
            select(PackageVersion.id, PackageVersion.version, PackageVersion.status, PackageVersion.deprecation)
            .where(PackageVersion.package_id == package.id)
        )
    ).all()
    source_rows = (
        await db.execute(
            select(PackageVersion.version, VersionSource.registry_id)
            .join(VersionSource, VersionSource.version_id == PackageVersion.id)
            .where(PackageVersion.package_id == package.id)
        )
    ).all()
    # A source the package went missing from counts as new when it answers again: one package-level event.
    registry_ids = (
        await db.scalars(
            select(PackageRegistry.registry_id)
            .where(PackageRegistry.package_id == package.id, PackageRegistry.missing_since.is_(None))
        )
    ).all()
    vuln_rows = (
        await db.execute(
            select(Vulnerability.source_id, Vulnerability.withdrawn_at)
            .join(PackageVulnerability, PackageVulnerability.vulnerability_id == Vulnerability.id)
            .where(PackageVulnerability.package_id == package.id)
        )
    ).all()
    # Only versions with version_sources rows: those written by 0005-era code before 0007 have none, and "no rows"
    # means "unknown", not "in no source" (no source_added for them). Rows of every status count as known.
    version_sources: dict[str, set[str]] = {}
    for row in source_rows:
        version_sources.setdefault(row.version, set()).add(str(row.registry_id))
    return PackageSnapshot(
        latest_version=package.latest_version,
        version_statuses={row.version: row.status for row in version_rows},
        vulnerability_withdrawn={row.source_id: row.withdrawn_at for row in vuln_rows},
        unavailable=package.unavailable_at is not None,
        registry_ids={str(registry_id) for registry_id in registry_ids},
        version_sources=version_sources,
        deprecated_versions={row.version for row in version_rows if row.deprecation is not None},
    )


def _latest_version(plugin: EcosystemPlugin, merged: MergedPackage) -> str | None:
    """The `latest` dist-tag if a source has one, else the highest valid stable active version."""
    tags = merged.package.dist_tags or {}
    if tags.get("latest"):
        return tags["latest"]
    candidates = [m.version.version for m in merged.versions if m.version.status == "active" and _is_valid(plugin, m.version.version)]
    stable = [v for v in candidates if not plugin.versions.is_prerelease(v)]
    pool = stable or candidates
    return max(pool, key=plugin.versions.sort_key) if pool else None


def _is_valid(plugin: EcosystemPlugin, version: str) -> bool:
    try:
        plugin.versions.validate(version)
    except InvalidVersion:
        return False
    return True


# --- package --------------------------------------------------------------------------------------


_KEPT_WHEN_MISSING = frozenset({
    "description", "homepage_url", "repository_url", "documentation_url", "license_raw", "license_spdx", "keywords",
    "downloads_total", "downloads_recent",
})


async def _upsert_package(
    db: AsyncSession,
    plugin: EcosystemPlugin,
    merged: MergedPackage,
    latest: str | None,
    origin: PackageOrigin,
    now: datetime,
    existing: Package | None,
) -> Package:
    data = merged.package
    values = {
        "name": data.display_name,
        "primary_registry_id": merged.primary_registry_id,
        "description": data.description,
        "homepage_url": data.homepage_url,
        "repository_url": data.repository_url,
        "documentation_url": data.documentation_url,
        "license_raw": data.license_raw,
        "license_spdx": data.license_spdx,
        "keywords": data.keywords,
        "latest_version": latest,
        "downloads_total": data.downloads_total,
        "downloads_recent": data.downloads_recent,
        "updated_at": data.updated_at or now,
        "synced_at": now,
        "raw_metadata": dict(data.raw_metadata),  # primary source only (IMPL T8)
        "deprecation": dict(data.deprecation) if data.deprecation is not None else None,
        "unavailable_at": None,
    }

    if existing:
        for key, value in values.items():
            # None = no answering source provides the field (an index with files only): keep what is stored.
            if value is None and key in _KEPT_WHEN_MISSING:
                continue
            setattr(existing, key, value)
        return existing

    package = Package(
        ecosystem=Ecosystem(data.ref.ecosystem),
        namespace=data.ref.namespace,
        normalized_name=data.ref.name,
        origin=origin,
        **values,
    )
    db.add(package)
    await db.flush()
    return package


async def _upsert_package_registries(
    db: AsyncSession, package: Package, merged: MergedPackage, missing_registry_ids: Sequence[str], now: datetime
) -> None:
    for registry_id, revision in merged.registry_revisions.items():
        stmt = insert(PackageRegistry).values(
            package_id=package.id,
            registry_id=registry_id,
            ecosystem=package.ecosystem,
            first_seen_at=now,
            upstream_revision=revision,
            checked_at=now,
            missing_since=None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["package_id", "registry_id"],
            set_={
                "ecosystem": stmt.excluded.ecosystem,
                "upstream_revision": stmt.excluded.upstream_revision,
                "checked_at": now,
                "missing_since": None,
            },
        )
        await db.execute(stmt)

    if missing_registry_ids:
        await db.execute(
            update(PackageRegistry)
            .where(PackageRegistry.package_id == package.id, PackageRegistry.registry_id.in_(list(missing_registry_ids)))
            .values(
                checked_at=now,
                missing_since=func.coalesce(PackageRegistry.missing_since, now),
            )
        )


# --- versions -------------------------------------------------------------------------------------


async def _upsert_versions(
    db: AsyncSession, plugin: EcosystemPlugin, package_id, versions: Iterable[MergedVersion]
) -> tuple[dict[str, PackageVersion], set[str]]:
    """Returns the rows by version and the versions whose metadata source changed."""
    stored = {
        row.version: row
        for row in (await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package_id))).all()
    }
    rows: dict[str, PackageVersion] = {}
    metadata_moved: set[str] = set()
    for merged in versions:
        version = merged.version
        raw_metadata = dict(version.raw_metadata)
        try:
            plugin.versions.validate(version.version)
            sort_key = plugin.versions.sort_key(version.version)
            is_prerelease = plugin.versions.is_prerelease(version.version)
        except InvalidVersion:
            # IMPL T15: kept, sorted below every valid version, ignored by resolvers and "latest".
            raw_metadata["invalid_version"] = True
            sort_key, is_prerelease = fallback_sort_key(version.version), False
        engines = dict(version.engines) if version.engines is not None else None

        values = {
            "release_date": version.release_date,
            "status": PackageVersionStatus(version.status),
            "status_reason": version.status_reason,
            "deprecation": dict(version.deprecation) if version.deprecation is not None else None,
            "engines": engines,
            "raw_metadata": raw_metadata,
            "sort_key": sort_key,
            "is_prerelease": is_prerelease,
            "metadata_registry_id": merged.metadata_registry_id,
        }
        hydrated = version.dependencies is not None

        existing = stored.get(version.version)
        if existing:
            if existing.metadata_registry_id is not None and str(existing.metadata_registry_id) != merged.metadata_registry_id:
                metadata_moved.add(version.version)
            for key, value in values.items():
                setattr(existing, key, value)
            existing.features = _features(existing.features, version.features, hydrated)
            if hydrated:
                existing.dependencies_state = DependenciesState.hydrated
            rows[version.version] = existing
            continue

        row = PackageVersion(
            id=uuid.uuid4(),
            package_id=package_id,
            version=version.version,
            features=_features(None, version.features, hydrated),
            dependencies_state=DependenciesState.hydrated if hydrated else DependenciesState.pending,
            **values,
        )
        db.add(row)
        rows[version.version] = row
    await db.flush()
    return rows, metadata_moved


def _features(stored: dict | None, incoming: Mapping[str, object] | None, hydrated: bool) -> dict:
    merged = {**(stored or {}), **(incoming or {})}
    # features.dependencies_hydrated is read by the 0005-era code until 0007 (dependencies_state replaces it).
    if hydrated:
        merged["dependencies_hydrated"] = True
    else:
        merged.setdefault("dependencies_hydrated", False)
    return merged


async def _write_version_sources(
    db: AsyncSession,
    package_id,
    version_ids: Mapping[str, object],
    versions: Sequence[MergedVersion],
    source_versions: Mapping[str, Mapping[str, NormalizedVersion]],
    asked_registry_ids: Iterable[str],
    now: datetime,
) -> None:
    rows = []
    for merged in versions:
        for registry_id, status in merged.sources.items():
            source_version = source_versions[registry_id].get(merged.version.version)
            known = source_version is not None and source_version.dependencies is not None
            rows.append({
                "version_id": version_ids[merged.version.version], "registry_id": registry_id, "first_seen_at": now,
                "status": PackageVersionStatus(status),
                "metadata_digest": dependencies_digest(source_version.dependencies) if known else None,
            })
    for chunk in _chunks(rows):
        stmt = insert(VersionSource).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["version_id", "registry_id"],
            set_={
                "status": stmt.excluded.status,
                "metadata_digest": func.coalesce(stmt.excluded.metadata_digest, VersionSource.metadata_digest),
            },
        )
        await db.execute(stmt)

    # A source that answered but no longer lists a version: removed there (the aggregate follows the merge).
    for registry_id in asked_registry_ids:
        listed = [version_ids[m.version.version] for m in versions if registry_id in m.sources]
        await db.execute(
            update(VersionSource)
            .where(
                VersionSource.registry_id == registry_id,
                VersionSource.version_id.in_(select(PackageVersion.id).where(PackageVersion.package_id == package_id)),
                VersionSource.version_id.not_in(listed),
                VersionSource.status != PackageVersionStatus.removed,
            )
            .values(status=PackageVersionStatus.removed)
        )


def _chunks(rows: list[dict], size: int = 1000) -> Iterable[list[dict]]:
    # A multi-row INSERT is one statement; asyncpg allows 32767 bind parameters per statement.
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


async def _frozen_versions(
    db: AsyncSession, package_id, asked_registry_ids: Iterable[str], incoming: set[str]
) -> set[str]:
    """Versions no asked source returned that a source not asked (disabled, or no longer matching its routing)
    still lists. Such a source is frozen: its data stays as it was, nothing is removed and no events follow."""
    rows = await db.scalars(
        select(PackageVersion.version)
        .join(VersionSource, VersionSource.version_id == PackageVersion.id)
        .where(
            PackageVersion.package_id == package_id,
            VersionSource.registry_id.not_in(list(asked_registry_ids)),
            VersionSource.status != PackageVersionStatus.removed,
        )
        .distinct()
    )
    return {version for version in rows if version not in incoming}


async def _mark_missing_versions_removed(db: AsyncSession, package_id, present_versions: Iterable[str]) -> None:
    await db.execute(
        update(PackageVersion)
        .where(
            PackageVersion.package_id == package_id,
            PackageVersion.version.not_in(list(present_versions)),
            PackageVersion.status != PackageVersionStatus.removed,
        )
        .values(status=PackageVersionStatus.removed)
    )


# --- dependencies and artifacts ---------------------------------------------------------------------


async def _write_dependencies(
    db: AsyncSession,
    row: PackageVersion,
    merged: MergedVersion,
    source_versions: Mapping[str, Mapping[str, NormalizedVersion]],
    metadata_moved: bool,
) -> None:
    """Canonical set (variant_registry_id NULL) from the metadata source; a variant for every other source whose
    known set differs (Q17). A source with unknown dependencies (lazy) keeps what is stored."""
    canonical = merged.version.dependencies
    if canonical is not None:
        await _replace_dependency_rows(db, row.id, None, canonical)
        await db.execute(
            delete(Dependency).where(Dependency.version_id == row.id,
                                     Dependency.variant_registry_id == merged.metadata_registry_id)
        )
    elif metadata_moved:
        # The previous metadata source stopped publishing the version; the new one has not given its dependencies.
        # Its stored variant becomes the canonical set; without one the stale set waits for lazy hydration.
        variant = await db.scalar(
            select(func.count(Dependency.id)).where(Dependency.version_id == row.id,
                                                    Dependency.variant_registry_id == merged.metadata_registry_id)
        )
        if variant:
            await db.execute(delete(Dependency).where(Dependency.version_id == row.id, Dependency.variant_registry_id.is_(None)))
            await db.execute(
                update(Dependency)
                .where(Dependency.version_id == row.id, Dependency.variant_registry_id == merged.metadata_registry_id)
                .values(variant_registry_id=None)
            )
        else:
            row.dependencies_state = DependenciesState.pending
            row.features = {**(row.features or {}), "dependencies_hydrated": False}

    for registry_id in merged.sources:
        if registry_id == merged.metadata_registry_id:
            continue
        source_version = source_versions[registry_id].get(merged.version.version)
        if source_version is None or source_version.dependencies is None:
            continue
        variant = merged.dependency_variants.get(registry_id)
        if variant is None:
            await db.execute(
                delete(Dependency).where(Dependency.version_id == row.id, Dependency.variant_registry_id == registry_id)
            )
        else:
            await _replace_dependency_rows(db, row.id, registry_id, variant)


async def _replace_dependency_rows(
    db: AsyncSession, version_id, variant_registry_id: str | None, dependencies: Sequence[NormalizedDependency]
) -> None:
    variant_filter = (
        Dependency.variant_registry_id.is_(None) if variant_registry_id is None
        else Dependency.variant_registry_id == variant_registry_id
    )
    await db.execute(delete(Dependency).where(Dependency.version_id == version_id, variant_filter))
    for dep in dependencies:
        db.add(
            Dependency(
                version_id=version_id,
                variant_registry_id=variant_registry_id,
                dependency_ecosystem=Ecosystem(dep.target.ecosystem),
                dependency_namespace=dep.target.namespace,
                dependency_name=dep.target.name,  # normalized (IMPL T14); the original spelling stays in raw_spec
                version_constraint=dep.constraint,
                dependency_type=DependencyType(dep.dependency_type),
                extra_name=dep.extra_name,
                environment_markers=dict(dep.environment_markers) if dep.environment_markers is not None else None,
                raw_spec=dep.raw_spec,
                scope=dep.scope,
                alias=dep.alias,
                details=dict(dep.details) if dep.details is not None else None,
            )
        )


_CHECKSUM_COLUMNS = {"sha256": "checksum_sha256", "md5": "checksum_md5", "sha1": "checksum_sha1",
                     "sha512": "checksum_sha512", "integrity": "checksum_integrity"}


async def _replace_artifacts(
    db: AsyncSession, registry_id: str, artifacts_by_version: Mapping[object, Sequence[NormalizedArtifact]]
) -> None:
    """Only this source's artifacts of the versions it lists are replaced (§6.5 p. 4)."""
    version_ids = list(artifacts_by_version)
    for start in range(0, len(version_ids), 1000):
        await db.execute(
            delete(Artifact).where(Artifact.registry_id == registry_id, Artifact.version_id.in_(version_ids[start:start + 1000]))
        )
    rows = [
        {
            "id": uuid.uuid4(),
            "version_id": version_id,
            "registry_id": registry_id,
            "filename": artifact.filename,
            "download_url": artifact.download_url,
            "size_bytes": artifact.size_bytes,
            "artifact_type": artifact.artifact_type,
            "arch": artifact.arch,
            "platform_tags": dict(artifact.platform_tags) if artifact.platform_tags is not None else None,
            "has_metadata_file": artifact.has_metadata_file,
            **{column: artifact.checksums.get(kind) for kind, column in _CHECKSUM_COLUMNS.items()},
        }
        for version_id, artifacts in artifacts_by_version.items()
        for artifact in artifacts
    ]
    for chunk in _chunks(rows):
        await db.execute(insert(Artifact).values(chunk).on_conflict_do_nothing())


async def _replace_dist_tags(db: AsyncSession, package_id, tags: Mapping[str, str], now: datetime) -> None:
    await db.execute(delete(DistTag).where(DistTag.package_id == package_id, DistTag.tag.not_in(list(tags))))
    for tag, version in tags.items():
        stmt = insert(DistTag).values(package_id=package_id, tag=tag, version=version, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["package_id", "tag"], set_={"version": version, "updated_at": now}
        )
        await db.execute(stmt)


async def _upsert_authors(db: AsyncSession, package_id, authors) -> None:
    await db.execute(delete(PackageAuthor).where(PackageAuthor.package_id == package_id))

    for author in authors:
        if not author.name:
            continue

        author_row = await db.scalar(
            select(Author).where(Author.name == author.name, Author.email == author.email)
        )
        if not author_row:
            author_row = Author(name=author.name, email=author.email, url=author.url)
            db.add(author_row)
            await db.flush()

        db.add(PackageAuthor(package_id=package_id, author_id=author_row.id, role=author.role))


async def _upsert_vulnerabilities(db: AsyncSession, package_id, vulnerabilities) -> None:
    await db.execute(delete(PackageVulnerability).where(PackageVulnerability.package_id == package_id))

    for item in vulnerabilities:
        vuln = await db.scalar(select(Vulnerability).where(Vulnerability.source_id == item.source_id))
        if not vuln:
            severity = None
            if item.severity:
                try:
                    severity = VulnerabilitySeverity(item.severity)
                except ValueError:
                    severity = VulnerabilitySeverity.unknown
            vuln = Vulnerability(
                source=VulnerabilitySource(item.source),
                source_id=item.source_id,
                aliases=item.aliases,
                summary=item.summary,
                details=item.details,
                severity=severity,
                fixed_in=item.fixed_in,
                published_at=item.published_at,
                withdrawn_at=item.withdrawn_at,
                link=item.link,
            )
            db.add(vuln)
            await db.flush()
        else:
            vuln.aliases = item.aliases
            vuln.fixed_in = item.fixed_in
            vuln.withdrawn_at = item.withdrawn_at

        db.add(PackageVulnerability(package_id=package_id, vulnerability_id=vuln.id, affected_versions=None))


# --- lazy dependencies of one version -------------------------------------------------------------------


async def ensure_version_dependencies(db: AsyncSession, package: Package, version_row: PackageVersion) -> None:
    """Load the canonical dependencies of a version that only has its row (lazy, SPEC §7) from its metadata source."""
    if version_row.dependencies_state != DependenciesState.pending:
        return

    plugin = plugin_for(package.ecosystem.value)
    ref = PackageRef(ecosystem=package.ecosystem.value, namespace=package.namespace or "", name=package.normalized_name)
    registry = await _dependencies_source(db, package, version_row)
    if registry is None:
        return   # only frozen (disabled) sources publish it: nothing to ask, the state stays pending
    registry_id = registry.id
    # A disabled metadata source hands the version over to the next enabled one, as the next merge would.
    version_row.metadata_registry_id = registry_id
    adapter = plugin.adapter(sources.registry_config(registry))
    loaded = await adapter.fetch_version_dependencies(ref, version_row.version, _ProfileLookup(plugin, [adapter.registry]))
    await store_documents(db, registry_id, loaded.documents)

    features = dict(version_row.features or {})
    if not loaded.available:
        version_row.dependencies_state = DependenciesState.unavailable
        await db.flush()
        return

    await _replace_dependency_rows(db, version_row.id, None, loaded.dependencies)
    if loaded.engines is not None:
        version_row.engines = {**(version_row.engines or {}), **loaded.engines}
    version_row.features = {**features, "dependencies_hydrated": True}
    version_row.dependencies_state = DependenciesState.hydrated
    await db.execute(
        update(VersionSource)
        .where(VersionSource.version_id == version_row.id, VersionSource.registry_id == registry_id)
        .values(metadata_digest=dependencies_digest(loaded.dependencies))
    )
    await db.flush()
    await resolve_dependency_links(db, [version_row.id])


# version_sources.metadata_digest of a source that has no metadata for the version (PEP 503 without PEP 658): its
# dependencies are unknown, whatever other sources say (Q14). Not a sha256, so it never equals a real digest.
UNAVAILABLE_DIGEST = b"unavailable"


async def source_dependencies(
    db: AsyncSession, package: Package, version_row: PackageVersion, registry_id: str
) -> tuple[list[Dependency] | None, bool]:
    """The version's dependencies as `registry_id` publishes them (SPEC §6.5, §9.1), and whether anything was loaded.

    The metadata source gives the canonical set. Another source is read lazily the first time a reader takes the
    version from it: the same metadata leaves only its digest, different metadata becomes a variant (torch 2.9.1 on
    the PyTorch CPU index has no nvidia-* requirements), none makes them unknown. None = unknown.
    """
    version_id, before = version_row.id, version_row.dependencies_state
    await ensure_version_dependencies(db, package, version_row)
    changed = version_row.dependencies_state != before
    canonical = version_row.dependencies_state == DependenciesState.hydrated
    if registry_id != str(version_row.metadata_registry_id):
        source = await db.scalar(
            select(VersionSource).where(VersionSource.version_id == version_id,
                                        VersionSource.registry_id == uuid.UUID(registry_id))
        )
        if source is not None:
            if source.metadata_digest is None:
                await _load_source_dependencies(db, package, version_row, source)
                changed = True
            if source.metadata_digest == UNAVAILABLE_DIGEST:
                return None, changed
            variant = (await db.scalars(
                select(Dependency).where(Dependency.version_id == version_id,
                                         Dependency.variant_registry_id == uuid.UUID(registry_id))
            )).all()
            if variant:
                return list(variant), changed
    if not canonical:
        return None, changed
    rows = (await db.scalars(
        select(Dependency).where(Dependency.version_id == version_id, Dependency.variant_registry_id.is_(None))
    )).all()
    return list(rows), changed


async def _load_source_dependencies(
    db: AsyncSession, package: Package, version_row: PackageVersion, source: VersionSource
) -> None:
    registry = await db.get(Registry, source.registry_id)
    if registry is None or not registry.enabled:
        return   # frozen: nothing to ask; readers never take a version from a disabled source anyway
    plugin = plugin_for(package.ecosystem.value)
    ref = PackageRef(ecosystem=package.ecosystem.value, namespace=package.namespace or "", name=package.normalized_name)
    adapter = plugin.adapter(sources.registry_config(registry))
    loaded = await adapter.fetch_version_dependencies(ref, version_row.version, _ProfileLookup(plugin, [adapter.registry]))
    await store_documents(db, registry.id, loaded.documents)
    if not loaded.available:
        source.metadata_digest = UNAVAILABLE_DIGEST
        await db.flush()
        return
    digest = dependencies_digest(loaded.dependencies)
    canonical_digest = await db.scalar(
        select(VersionSource.metadata_digest).where(VersionSource.version_id == version_row.id,
                                                    VersionSource.registry_id == version_row.metadata_registry_id)
    )
    if digest != canonical_digest:
        await _replace_dependency_rows(db, version_row.id, str(registry.id), loaded.dependencies)
    source.metadata_digest = digest
    await db.flush()
    await resolve_dependency_links(db, [version_row.id])


async def store_documents(db: AsyncSession, registry_id, documents: Sequence[FetchedDocument]) -> None:
    """Upstream documents the metadata came from (source_documents), replaced when fetched again."""
    now = datetime.now(UTC)
    for document in documents:
        values = {
            "content": document.content, "content_type": document.content_type[:64],
            "sha256": hashlib.sha256(document.content).hexdigest(), "etag": document.etag, "fetched_at": now,
        }
        stmt = insert(SourceDocument).values(registry_id=registry_id, path=document.path, **values)
        await db.execute(stmt.on_conflict_do_update(index_elements=["registry_id", "path"], set_=values))


async def _dependencies_source(db: AsyncSession, package: Package, version_row: PackageVersion) -> Registry | None:
    """The version's metadata source if enabled; otherwise the highest-priority enabled source that lists it."""
    candidates = (
        await db.scalars(
            select(Registry)
            .join(VersionSource, VersionSource.registry_id == Registry.id)
            .where(VersionSource.version_id == version_row.id, VersionSource.status != PackageVersionStatus.removed,
                   Registry.enabled.is_(True), Registry.protocol.is_not(None))
            .order_by(Registry.priority, Registry.slug)
        )
    ).all()
    plugin = plugin_for(package.ecosystem.value)
    ref = PackageRef(ecosystem=package.ecosystem.value, namespace=package.namespace or "", name=package.normalized_name)
    # Routing freezes sources like disabling does: an excluded source does not describe the version either.
    allowed = {r.id for r in plugin.sources_for(ref, SourceProfile(ecosystem=ref.ecosystem, slug="catalog",
                                                                   registries=[sources.registry_config(c) for c in candidates]))}
    by_id = {str(c.id): c for c in candidates if str(c.id) in allowed}
    preferred = str(version_row.metadata_registry_id or package.primary_registry_id)
    return by_id.get(preferred) or next(iter(by_id.values()), None)


# --- level-1 links ---------------------------------------------------------------------------------------


async def resolve_dependency_links(db: AsyncSession, version_ids: Iterable[object]) -> None:
    """Point each dependency at the catalog package with the same (ecosystem, namespace, name) and the best
    version satisfying its constraint."""
    picker = _VersionPicker(db)
    ids = list(version_ids)
    for start in range(0, len(ids), 1000):
        dep_rows = (await db.scalars(select(Dependency).where(Dependency.version_id.in_(ids[start:start + 1000])))).all()
        for dep in dep_rows:
            target = await picker.package(
                PackageRef(ecosystem=dep.dependency_ecosystem.value, namespace=dep.dependency_namespace or "",
                           name=dep.dependency_name)
            )
            dep.resolved_package_id = target.id if target else None
            dep.resolved_version_id = await picker.best(target, dep.version_constraint) if target else None


async def _link_dependents(db: AsyncSession, package: Package) -> None:
    """Point already stored dependencies at a package that just entered the catalog."""
    picker = _VersionPicker(db)
    dep_rows = (
        await db.scalars(
            select(Dependency).where(
                Dependency.dependency_ecosystem == package.ecosystem,
                func.coalesce(Dependency.dependency_namespace, "") == (package.namespace or ""),
                Dependency.dependency_name == package.normalized_name,
                Dependency.resolved_package_id.is_(None),
            )
        )
    ).all()
    for dep in dep_rows:
        dep.resolved_package_id = package.id
        dep.resolved_version_id = await picker.best(package, dep.version_constraint)


class _VersionPicker:
    """Level-1 link targets for one write: every target package and its versions are loaded and parsed once,
    and the answer per (package, constraint) is reused. A refresh of boto3 links hundreds of hydrated versions to
    botocore with thousands of versions; without this that is a query and a full scan per dependency."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._packages: dict[PackageRef, Package | None] = {}
        self._candidates: dict[object, list[tuple[str, object, bool]]] = {}
        self._best: dict[tuple[object, str], object] = {}

    async def package(self, ref: PackageRef) -> Package | None:
        if ref not in self._packages:
            self._packages[ref] = await find_package(self.db, ref)
        return self._packages[ref]

    async def best(self, package: Package, constraint: str | None):
        key = (package.id, constraint or "")
        if key not in self._best:
            if package.id not in self._candidates:
                rows = (
                    await self.db.execute(
                        select(PackageVersion.version, PackageVersion.id).where(
                            PackageVersion.package_id == package.id,
                            PackageVersion.status == PackageVersionStatus.active,
                        )
                    )
                ).all()
                self._candidates[package.id] = _candidates(plugin_for(package.ecosystem.value), rows)
            self._best[key] = _pick(plugin_for(package.ecosystem.value), self._candidates[package.id], constraint)
        return self._best[key]


def _candidates(plugin: EcosystemPlugin, versions: Iterable[tuple[str, object]]) -> list[tuple[str, object, bool]]:
    """Valid versions, highest first, with their prerelease flag."""
    valid = [(version, row_id) for version, row_id in versions if _is_valid(plugin, version)]
    valid.sort(key=lambda item: plugin.versions.sort_key(item[0]), reverse=True)
    return [(version, row_id, plugin.versions.is_prerelease(version)) for version, row_id in valid]


def _pick(plugin: EcosystemPlugin, candidates: Sequence[tuple[str, object, bool]], constraint: str | None):
    try:
        spec = plugin.versions.constraint(constraint or "*")
    except InvalidVersion:
        spec = plugin.versions.constraint("*")
    fallback = None
    for version, row_id, prerelease in candidates:
        if not spec.contains(version):
            continue
        if not prerelease:
            return row_id
        fallback = fallback or row_id
    return fallback


def select_best_version(plugin: EcosystemPlugin, versions: Iterable[tuple[str, object]], constraint: str | None):
    """Highest valid version satisfying the constraint, stable ones first; an unparsable constraint matches any."""
    return _pick(plugin, _candidates(plugin, versions), constraint)


# --- events and source state ---------------------------------------------------------------------------


async def _record_events(db: AsyncSession, package_id, events: list[EventDraft], run_id, now: datetime) -> None:
    for event in events:
        db.add(
            PackageEvent(
                package_id=package_id,
                version=event.version,
                kind=event.kind,
                detected_at=now,
                occurred_at=event.occurred_at,
                run_id=run_id,
                details=event.details,
            )
        )


async def _upsert_sync_state(db: AsyncSession, registry_id, now: datetime) -> None:
    row = await db.scalar(select(SyncState).where(SyncState.registry_id == registry_id))
    if row:
        row.status = SyncStatus.idle
        row.last_success_at = now
        row.last_error = None
        # An answer, also "no such package", closes the circuit breaker.
        row.consecutive_failures = 0
        row.degraded_until = None
        return

    db.add(
        SyncState(
            registry_id=registry_id,
            status=SyncStatus.idle,
            last_success_at=now,
            last_error=None,
            cursor_data={},
        )
    )


async def get_or_create_pypi_registry(db: AsyncSession) -> Registry:
    """The starter source of PyPI (IMPL R4); 0006 creates it, this covers a database built by hand."""
    registry = await db.scalar(select(Registry).where(Registry.slug == "pypi"))
    if registry:
        return registry

    registry = Registry(
        ecosystem=Ecosystem.pypi,
        name="PyPI",
        slug="pypi",
        base_url="https://pypi.org/",
        protocol=RegistryProtocol.pypi_warehouse,
        priority=0,
        enabled=True,
    )
    db.add(registry)
    await db.flush()
    return registry
