from datetime import UTC, datetime
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import (
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
)
from app.connectors.pypi import PyPIConnector
from app.models.artifact import Artifact, DistTag
from app.models.author import Author, PackageAuthor
from app.models.dependency import Dependency
from app.models.enums import (
    DependencyType,
    Ecosystem,
    PackageVersionStatus,
    RegistryType,
    SyncStatus,
    SyncType,
    Tier,
    VulnerabilitySeverity,
    VulnerabilitySource,
)
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.models.vulnerability import PackageVulnerability, Vulnerability
from app.normalizers.names import normalize_name, normalize_package_name
from app.probe.enqueue import schedule_latest_probe

# Дефолтные публичные реестры: строка создаётся при первом обращении.
DEFAULT_REGISTRY_PRESETS = {
    "pypi": {
        "slug": "pypi",
        "name": "PyPI",
        "protocol": "pypi-json",
        "base_url": "https://pypi.org/",
        "api_url": "https://pypi.org/pypi/",
    },
    "npm": {
        "slug": "npm",
        "name": "npm Registry",
        "protocol": "npm",
        "base_url": "https://registry.npmjs.org/",
        "api_url": None,
    },
}

# D12: metadata flows from the richest protocol; a poorer protocol never
# overwrites fields obtained from a richer one.
PROTOCOL_RICHNESS = {"pypi-json": 2, "npm": 2, "pypi-simple": 1}


def protocol_richness(protocol: str | None) -> int:
    return PROTOCOL_RICHNESS.get(protocol or "", 0)


async def run_sync_pipeline(
    package_data: NormalizedPackage,
    db: AsyncSession,
    registry: Registry | None = None,
) -> Package:
    now = datetime.now(UTC)

    if registry is None:
        registry = await get_or_create_default_registry(db, package_data.ecosystem)

    package, allow_update = await _upsert_package(db, registry, package_data, now)
    await _upsert_package_registry(db, package.id, registry, package_data.name, now)
    if allow_update:
        await _promote_primary(db, package, registry)

    version_id_by_version = await _upsert_versions(
        db, package.id, package_data.versions, allow_update=allow_update
    )

    for version in package_data.versions:
        version_id = version_id_by_version.get(version.version)
        if version_id is None:
            continue
        if version.dependencies or version.dependencies_hydrated:
            await _replace_dependencies(db, version_id, version.dependencies, package_data.ecosystem)
        await _replace_artifacts(db, version_id, registry.id, version)

    if allow_update:
        dist_tags = package_data.dist_tags or {"latest": package_data.latest_version}
        for tag, tagged_version in dist_tags.items():
            await _upsert_dist_tag(db, package.id, tag, tagged_version, now)
        await _upsert_authors(db, package.id, package_data.authors)
        await _upsert_vulnerabilities(db, package.id, package_data.vulnerabilities)

    await _update_collision_flags(db, package.id, list(version_id_by_version.values()))
    await _resolve_level1(db, version_id_by_version.values())
    await _upsert_sync_state(db, registry.id, now)

    await db.commit()
    await db.refresh(package)
    if allow_update:
        schedule_latest_probe(
            package_data.latest_version,
            {version: str(version_id) for version, version_id in version_id_by_version.items()},
        )
    return package


async def find_package(db: AsyncSession, ecosystem: str, normalized_name: str) -> Package | None:
    result = await db.scalar(
        select(Package).where(Package.ecosystem == Ecosystem(ecosystem), Package.normalized_name == normalized_name)
    )
    return result


async def get_or_create_default_registry(db: AsyncSession, ecosystem: str) -> Registry:
    preset = DEFAULT_REGISTRY_PRESETS.get(ecosystem)
    if preset is None:
        raise ValueError(f"no default registry preset for ecosystem '{ecosystem}'")

    registry = await db.scalar(select(Registry).where(Registry.slug == preset["slug"]))
    if registry:
        return registry

    registry = Registry(
        ecosystem=Ecosystem(ecosystem),
        name=preset["name"],
        slug=preset["slug"],
        registry_type=RegistryType.public,
        protocol=preset["protocol"],
        base_url=preset["base_url"],
        api_url=preset["api_url"],
        tier=Tier.tier1,
        sync_enabled=True,
        sync_priority=0,
    )
    db.add(registry)
    await db.flush()
    return registry


async def connector_for_package(db: AsyncSession, package: Package):
    """Connector for the package's primary registry (falls back to the
    ecosystem's default public registry preset)."""
    from app.connectors.factory import build_connector, registry_info_from_row

    registry = await _current_primary_registry(db, package.id)
    if registry is None:
        registry = await get_or_create_default_registry(db, package.ecosystem.value)
    return build_connector(registry_info_from_row(registry))


async def ensure_version_row(db: AsyncSession, package: Package, version_str: str) -> PackageVersion:
    """Guarantee a version row that carries a classifier-ready manifest.

    Creates the row if the version is missing (published after last sync);
    for pypi also backfills the version-scoped manifest ({info, urls} of THIS
    version) when the row was created by bulk sync with only {files}. Absent
    version upstream -> PackageNotFound (facts service maps to 404).
    """
    ecosystem = package.ecosystem.value
    row = await db.scalar(
        select(PackageVersion).where(
            PackageVersion.package_id == package.id, PackageVersion.version == version_str
        )
    )
    # pypi историческая версия из bulk-sync хранит только {files} — info для
    # классификатора нет; такую строку дозагружаем version-scoped-манифестом.
    needs_manifest = row is None or (ecosystem == "pypi" and not (row.raw_metadata or {}).get("info"))
    if not needs_manifest:
        return row

    from app.connectors.factory import build_connector, registry_info_from_row

    registry = await _current_primary_registry(db, package.id)
    if registry is None:
        registry = await get_or_create_default_registry(db, ecosystem)
    connector = build_connector(registry_info_from_row(registry))

    normalized_version = await connector.fetch_version_metadata(package.normalized_name, version_str)
    version_ids = await _upsert_versions(db, package.id, [normalized_version], allow_update=True)
    version_id = version_ids[version_str]
    await _replace_artifacts(db, version_id, registry.id, normalized_version)
    if normalized_version.dependencies:
        await _replace_dependencies(db, version_id, normalized_version.dependencies, ecosystem)
    await _resolve_level1(db, [version_id])
    await db.flush()
    return await db.get(PackageVersion, version_id)


async def _current_primary_registry(db: AsyncSession, package_id) -> Registry | None:
    return await db.scalar(
        select(Registry)
        .join(PackageRegistry, PackageRegistry.registry_id == Registry.id)
        .where(PackageRegistry.package_id == package_id, PackageRegistry.is_primary.is_(True))
    )


def _apply_package_fields(package: Package, package_data: NormalizedPackage, now: datetime) -> None:
    package.name = package_data.name
    package.namespace = package_data.namespace
    package.deprecated = package_data.deprecated
    package.deprecation_message = package_data.deprecation_message
    package.description = package_data.description
    package.homepage_url = package_data.homepage_url
    package.repository_url = package_data.repository_url
    package.documentation_url = package_data.documentation_url
    package.license_raw = package_data.license_raw
    package.license_spdx = package_data.license_spdx
    package.keywords = package_data.keywords
    package.latest_version = package_data.latest_version
    package.downloads_total = package_data.downloads_total
    package.downloads_recent = package_data.downloads_recent
    package.updated_at = package_data.updated_at
    package.synced_at = now
    package.raw_metadata = package_data.raw_metadata


async def _upsert_package(
    db: AsyncSession,
    registry: Registry,
    package_data: NormalizedPackage,
    now: datetime,
) -> tuple[Package, bool]:
    """Returns (package, allow_update): whether this sync owns package metadata.

    The syncing registry owns metadata when it is (or becomes) the primary:
    it already is primary, the package is new, or its protocol is strictly
    richer than the current primary's (promotion, D12).
    """
    existing = await find_package(db, package_data.ecosystem, package_data.normalized_name)
    if existing:
        primary = await _current_primary_registry(db, existing.id)
        is_primary = primary is not None and primary.id == registry.id
        richer = protocol_richness(registry.protocol) > protocol_richness(primary.protocol if primary else None)
        allow_update = is_primary or primary is None or richer
        if allow_update:
            _apply_package_fields(existing, package_data, now)
        return existing, allow_update

    package = Package(
        ecosystem=Ecosystem(package_data.ecosystem),
        namespace=package_data.namespace,
        name=package_data.name,
        normalized_name=package_data.normalized_name,
        primary_registry_id=registry.id,
        description=package_data.description,
        homepage_url=package_data.homepage_url,
        repository_url=package_data.repository_url,
        documentation_url=package_data.documentation_url,
        license_raw=package_data.license_raw,
        license_spdx=package_data.license_spdx,
        keywords=package_data.keywords,
        latest_version=package_data.latest_version,
        downloads_total=package_data.downloads_total,
        downloads_recent=package_data.downloads_recent,
        updated_at=package_data.updated_at,
        synced_at=now,
        raw_metadata=package_data.raw_metadata,
    )
    db.add(package)
    await db.flush()
    return package, True


async def _upsert_package_registry(db: AsyncSession, package_id, registry: Registry, name: str, now: datetime) -> None:
    if registry.slug == "pypi":
        source_url = f"https://pypi.org/project/{name}/"
    else:
        source_url = f"{registry.base_url.rstrip('/')}/{normalize_package_name(name)}/"
    stmt = insert(PackageRegistry).values(
        package_id=package_id,
        registry_id=registry.id,
        first_seen_at=now,
        source_url=source_url,
        is_primary=False,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["package_id", "registry_id"])
    await db.execute(stmt)


async def _promote_primary(db: AsyncSession, package: Package, registry: Registry) -> None:
    links = (
        await db.scalars(select(PackageRegistry).where(PackageRegistry.package_id == package.id))
    ).all()
    for link in links:
        link.is_primary = link.registry_id == registry.id
    package.primary_registry_id = registry.id
    await db.flush()


async def _upsert_versions(
    db: AsyncSession,
    package_id,
    versions: Iterable[NormalizedVersion],
    *,
    allow_update: bool = True,
) -> dict[str, str]:
    """Version rows are the union across registries: a secondary registry may
    add versions unknown to the primary (torch 2.1.0+cu121), but only the
    metadata owner updates existing rows."""
    version_ids: dict[str, str] = {}
    for version in versions:
        existing = await db.scalar(
            select(PackageVersion).where(PackageVersion.package_id == package_id, PackageVersion.version == version.version)
        )

        if existing:
            if allow_update:
                existing.release_date = version.release_date
                existing.status = PackageVersionStatus(version.status)
                existing.requires_python = version.requires_python
                existing.features = _merge_version_features(existing.features, version.dependencies_hydrated)
                existing.raw_metadata = version.raw_metadata
            version_ids[version.version] = existing.id
            continue

        new_row = PackageVersion(
            package_id=package_id,
            version=version.version,
            release_date=version.release_date,
            status=PackageVersionStatus(version.status),
            requires_python=version.requires_python,
            features=_merge_version_features(None, version.dependencies_hydrated),
            raw_metadata=version.raw_metadata,
        )
        db.add(new_row)
        await db.flush()
        version_ids[version.version] = new_row.id

    return version_ids


def _merge_version_features(features: dict | None, dependencies_hydrated: bool) -> dict:
    merged = dict(features or {})
    if dependencies_hydrated:
        merged["dependencies_hydrated"] = True
    else:
        merged.setdefault("dependencies_hydrated", False)
    return merged


async def _replace_dependencies(
    db: AsyncSession, version_id, dependencies: list[NormalizedDependency], ecosystem: str = "pypi"
) -> None:
    await db.execute(delete(Dependency).where(Dependency.version_id == version_id))

    for dep in dependencies:
        db.add(
            Dependency(
                version_id=version_id,
                dependency_name=dep.dependency_name,
                dependency_namespace=None,
                dependency_ecosystem=Ecosystem(ecosystem),
                version_constraint=dep.version_constraint,
                dependency_type=DependencyType(dep.dependency_type),
                extra_name=dep.extra_name,
                environment_markers=dep.environment_markers,
                raw_spec=dep.raw_spec,
            )
        )


async def ensure_version_dependencies(
    db: AsyncSession,
    package: Package,
    version_row: PackageVersion,
    connector: PyPIConnector,
) -> None:
    features = dict(version_row.features or {})
    if features.get("dependencies_hydrated") is True:
        return

    dep_count = await db.scalar(select(func.count(Dependency.id)).where(Dependency.version_id == version_row.id))
    if dep_count and features.get("dependencies_hydrated") is None:
        version_row.features = {**features, "dependencies_hydrated": True}
        await db.flush()
        return

    dependencies, requires_python = await connector.fetch_version_dependencies(package.normalized_name, version_row.version)
    await _replace_dependencies(db, version_row.id, dependencies, package.ecosystem.value)
    if requires_python:
        version_row.requires_python = requires_python
    version_row.features = {**features, "dependencies_hydrated": True}
    await _resolve_level1(db, [version_row.id])
    await db.flush()


async def _replace_artifacts(db: AsyncSession, version_id, registry_id, version: NormalizedVersion) -> None:
    """Scoped to the syncing registry: another registry's artifacts of the
    same version must survive (multi-registry union, D12)."""
    await db.execute(
        delete(Artifact).where(Artifact.version_id == version_id, Artifact.registry_id == registry_id)
    )

    for artifact in version.artifacts:
        db.add(
            Artifact(
                version_id=version_id,
                registry_id=registry_id,
                filename=artifact.filename,
                download_url=artifact.download_url,
                size_bytes=artifact.size_bytes,
                checksum_sha256=artifact.checksum_sha256,
                checksum_md5=artifact.checksum_md5,
                checksum_sha1=artifact.checksum_sha1,
                checksum_integrity=artifact.checksum_integrity,
                artifact_type=artifact.artifact_type,
                platform_tags=artifact.platform_tags,
                requires_dist_metadata=artifact.requires_dist_metadata,
            )
        )


CHECKSUM_ATTRS = ("checksum_sha256", "checksum_integrity", "checksum_sha1")


def find_checksum_conflicts(rows: Iterable) -> set:
    """Registry ids whose artifact for the same (version, filename) carries a
    different checksum than another registry's copy (D12 flag).

    Comparison is per checksum kind: a mismatch is flagged only when two
    registries publish the SAME kind of hash with different values — a
    registry that offers only sha1 never falsely conflicts with one that
    offers only sha256.
    """
    by_key: dict[tuple, list] = {}
    for row in rows:
        checksums = {attr: getattr(row, attr, None) for attr in CHECKSUM_ATTRS}
        if not any(checksums.values()):
            continue
        by_key.setdefault((row.version_id, row.filename), []).append((row.registry_id, checksums))

    conflicted: set = set()
    for entries in by_key.values():
        for i, (registry_a, sums_a) in enumerate(entries):
            for registry_b, sums_b in entries[i + 1 :]:
                for attr in CHECKSUM_ATTRS:
                    if sums_a[attr] and sums_b[attr] and sums_a[attr] != sums_b[attr]:
                        conflicted.update((registry_a, registry_b))
                        break
    return conflicted


async def _update_collision_flags(db: AsyncSession, package_id, version_ids: list) -> None:
    links = (
        await db.scalars(select(PackageRegistry).where(PackageRegistry.package_id == package_id))
    ).all()
    if not links:
        return
    registries = {
        r.id: r
        for r in (
            await db.scalars(select(Registry).where(Registry.id.in_([link.registry_id for link in links])))
        ).all()
    }

    types = {registries[link.registry_id].registry_type for link in links if link.registry_id in registries}
    if RegistryType.corporate in types and RegistryType.public in types:
        for link in links:
            link.name_collision = True

    if version_ids:
        artifact_rows = (
            await db.scalars(select(Artifact).where(Artifact.version_id.in_(version_ids)))
        ).all()
        conflicted_registries = find_checksum_conflicts(artifact_rows)
        for link in links:
            if link.registry_id in conflicted_registries:
                link.checksum_mismatch = True
    await db.flush()


async def _upsert_dist_tag(db: AsyncSession, package_id, tag: str, version: str, now: datetime) -> None:
    stmt = insert(DistTag).values(package_id=package_id, tag=tag, version=version, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["package_id", "tag"],
        set_={"version": version, "updated_at": now},
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

        db.add(PackageVulnerability(package_id=package_id, vulnerability_id=vuln.id, affected_versions=None))


async def _resolve_level1(db: AsyncSession, version_ids: Iterable[str]) -> None:
    dep_rows = (
        await db.scalars(select(Dependency).where(Dependency.version_id.in_(list(version_ids))))
    ).all()

    for dep in dep_rows:
        dep_ecosystem = dep.dependency_ecosystem.value if hasattr(dep.dependency_ecosystem, "value") else str(dep.dependency_ecosystem)
        candidate_pkg = await db.scalar(
            select(Package).where(
                Package.ecosystem == dep.dependency_ecosystem,
                Package.normalized_name == normalize_name(dep_ecosystem, dep.dependency_name),
            )
        )
        dep.resolved_package_id = candidate_pkg.id if candidate_pkg else None

        if not candidate_pkg:
            dep.resolved_version_id = None
            continue

        # npm: node-semver диапазоны не парсятся PEP 440 — берётся latest stable
        # (задокументированное ограничение Level-1 до npm-стратегии резолвера).
        constraint = dep.version_constraint if dep_ecosystem == "pypi" else None
        dep.resolved_version_id = await _find_best_version_id(db, candidate_pkg.id, constraint)


async def _find_best_version_id(db: AsyncSession, package_id, constraint: str | None):
    versions = (
        await db.scalars(
            select(PackageVersion).where(PackageVersion.package_id == package_id, PackageVersion.status == PackageVersionStatus.active)
        )
    ).all()

    return _select_best_version_id(versions, constraint)


def _select_best_version_id(versions: Iterable[PackageVersion], constraint: str | None):
    parsed_versions: list[tuple[Version, str]] = []
    for row in versions:
        try:
            parsed_versions.append((Version(row.version), row.id))
        except InvalidVersion:
            continue

    try:
        spec = SpecifierSet(constraint or "")
    except InvalidSpecifier:
        spec = SpecifierSet("")

    matches = _filter_matching_versions(parsed_versions, spec, allow_prereleases=False)
    if not matches:
        matches = _filter_matching_versions(parsed_versions, spec, allow_prereleases=True)
    if not matches:
        return None

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def _filter_matching_versions(
    parsed_versions: Iterable[tuple[Version, str]],
    spec: SpecifierSet,
    allow_prereleases: bool,
) -> list[tuple[Version, str]]:
    matches: list[tuple[Version, str]] = []
    prereleases = spec.prereleases if spec.prereleases is not None else allow_prereleases
    for ver, row_id in parsed_versions:
        if spec.contains(ver, prereleases=prereleases):
            matches.append((ver, row_id))
    return matches


async def resolve_dependency_links(db: AsyncSession, version_ids: Iterable[str]) -> None:
    await _resolve_level1(db, version_ids)


async def _upsert_sync_state(db: AsyncSession, registry_id, now: datetime) -> None:
    row = await db.scalar(select(SyncState).where(SyncState.registry_id == registry_id))
    if row:
        row.sync_type = SyncType.incremental
        row.status = SyncStatus.idle
        row.last_success_at = now
        row.last_error = None
        return

    db.add(
        SyncState(
            registry_id=registry_id,
            sync_type=SyncType.full,
            status=SyncStatus.idle,
            last_success_at=now,
            last_error=None,
            cursor_data={"last_serial": None},
            packages_synced=0,
            packages_total=0,
        )
    )
