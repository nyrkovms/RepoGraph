from datetime import UTC, datetime
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import (
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
)
from app.ecosystems.pypi.connector import PyPIConnector
from app.fetcher.events import EventDraft, PackageSnapshot, diff_package
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
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.sync_state import SyncState
from app.models.version import PackageVersion
from app.models.vulnerability import PackageVulnerability, Vulnerability
from app.normalizers.names import normalize_package_name


async def run_sync_pipeline(package_data: NormalizedPackage, db: AsyncSession, run_id=None) -> Package:
    now = datetime.now(UTC)

    registry = await _get_or_create_pypi_registry(db)
    existing = await find_package(db, "pypi", package_data.normalized_name)
    snapshot = await _load_snapshot(db, existing) if existing else None

    package = await _upsert_package(db, registry.id, package_data, now)
    await _upsert_package_registry(db, package.id, registry.id, package_data.name, now)

    version_id_by_version = await _upsert_versions(db, package.id, package_data.versions)
    await _mark_missing_versions_removed(db, package.id, version_id_by_version.keys())

    for version in package_data.versions:
        version_id = version_id_by_version[version.version]
        # Connector only carries dependencies for hydrated versions; leave lazily fetched ones intact.
        if version.dependencies_hydrated:
            await _replace_dependencies(db, version_id, version.dependencies)
        await _replace_artifacts(db, version_id, registry.id, version)

    await _upsert_dist_tag(db, package.id, package_data.latest_version, now)
    await _upsert_authors(db, package.id, package_data.authors)
    await _upsert_vulnerabilities(db, package.id, package_data.vulnerabilities)
    await _resolve_level1(db, version_id_by_version.values())
    if snapshot is None:
        await _link_dependents(db, package)
    await _record_events(db, package.id, diff_package(snapshot, package_data), run_id, now)
    await _upsert_sync_state(db, registry.id, now)

    await db.commit()
    await db.refresh(package)
    return package


async def find_package(db: AsyncSession, ecosystem: str, normalized_name: str) -> Package | None:
    result = await db.scalar(
        select(Package).where(Package.ecosystem == Ecosystem(ecosystem), Package.normalized_name == normalized_name)
    )
    return result


async def _load_snapshot(db: AsyncSession, package: Package) -> PackageSnapshot:
    version_rows = (
        await db.execute(select(PackageVersion.version, PackageVersion.status).where(PackageVersion.package_id == package.id))
    ).all()
    vuln_rows = (
        await db.execute(
            select(Vulnerability.source_id, Vulnerability.withdrawn_at)
            .join(PackageVulnerability, PackageVulnerability.vulnerability_id == Vulnerability.id)
            .where(PackageVulnerability.package_id == package.id)
        )
    ).all()
    return PackageSnapshot(
        latest_version=package.latest_version,
        version_statuses={row.version: row.status for row in version_rows},
        vulnerability_withdrawn={row.source_id: row.withdrawn_at for row in vuln_rows},
        unavailable=package.unavailable_at is not None,
    )


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


async def _link_dependents(db: AsyncSession, package: Package) -> None:
    """Point already stored dependencies at a package that just entered the catalog."""
    normalized_dep_name = func.lower(func.regexp_replace(Dependency.dependency_name, "[-_.]+", "-", "g"))
    dep_rows = (
        await db.scalars(
            select(Dependency).where(
                Dependency.dependency_ecosystem == Ecosystem.pypi,
                Dependency.resolved_package_id.is_(None),
                normalized_dep_name == package.normalized_name,
            )
        )
    ).all()
    for dep in dep_rows:
        dep.resolved_package_id = package.id
        dep.resolved_version_id = await _find_best_version_id(db, package.id, dep.version_constraint)


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


async def _get_or_create_pypi_registry(db: AsyncSession) -> Registry:
    registry = await db.scalar(select(Registry).where(Registry.slug == "pypi"))
    if registry:
        return registry

    registry = Registry(
        ecosystem=Ecosystem.pypi,
        name="PyPI",
        slug="pypi",
        registry_type=RegistryType.public,
        base_url="https://pypi.org/",
        api_url="https://pypi.org/pypi/",
        tier=Tier.tier1,
        sync_enabled=True,
        sync_priority=0,
    )
    db.add(registry)
    await db.flush()
    return registry


async def _upsert_package(db: AsyncSession, registry_id, package_data: NormalizedPackage, now: datetime) -> Package:
    existing = await find_package(db, "pypi", package_data.normalized_name)
    if existing:
        existing.name = package_data.name
        existing.description = package_data.description
        existing.homepage_url = package_data.homepage_url
        existing.repository_url = package_data.repository_url
        existing.documentation_url = package_data.documentation_url
        existing.license_raw = package_data.license_raw
        existing.license_spdx = package_data.license_spdx
        existing.keywords = package_data.keywords
        existing.latest_version = package_data.latest_version
        existing.downloads_total = package_data.downloads_total
        existing.downloads_recent = package_data.downloads_recent
        existing.updated_at = package_data.updated_at
        existing.synced_at = now
        existing.raw_metadata = package_data.raw_metadata
        if package_data.last_serial is not None:
            existing.last_serial = package_data.last_serial
        existing.unavailable_at = None
        return existing

    package = Package(
        ecosystem=Ecosystem.pypi,
        namespace=None,
        name=package_data.name,
        normalized_name=package_data.normalized_name,
        primary_registry_id=registry_id,
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
        last_serial=package_data.last_serial,
    )
    db.add(package)
    await db.flush()
    return package


async def _upsert_package_registry(db: AsyncSession, package_id, registry_id, name: str, now: datetime) -> None:
    stmt = insert(PackageRegistry).values(
        package_id=package_id,
        registry_id=registry_id,
        first_seen_at=now,
        source_url=f"https://pypi.org/project/{name}/",
        is_primary=True,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["package_id", "registry_id"])
    await db.execute(stmt)


async def _upsert_versions(db: AsyncSession, package_id, versions: Iterable[NormalizedVersion]) -> dict[str, str]:
    version_ids: dict[str, str] = {}
    for version in versions:
        existing = await db.scalar(
            select(PackageVersion).where(PackageVersion.package_id == package_id, PackageVersion.version == version.version)
        )

        if existing:
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


async def _replace_dependencies(db: AsyncSession, version_id, dependencies: list[NormalizedDependency]) -> None:
    await db.execute(delete(Dependency).where(Dependency.version_id == version_id))

    for dep in dependencies:
        db.add(
            Dependency(
                version_id=version_id,
                dependency_name=dep.dependency_name,
                dependency_namespace=None,
                dependency_ecosystem=Ecosystem.pypi,
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
    await _replace_dependencies(db, version_row.id, dependencies)
    if requires_python:
        version_row.requires_python = requires_python
    version_row.features = {**features, "dependencies_hydrated": True}
    await _resolve_level1(db, [version_row.id])
    await db.flush()


async def _replace_artifacts(db: AsyncSession, version_id, registry_id, version: NormalizedVersion) -> None:
    await db.execute(delete(Artifact).where(Artifact.version_id == version_id))

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
                artifact_type=artifact.artifact_type,
                platform_tags=artifact.platform_tags,
                requires_dist_metadata=artifact.requires_dist_metadata,
            )
        )


async def _upsert_dist_tag(db: AsyncSession, package_id, latest_version: str, now: datetime) -> None:
    stmt = insert(DistTag).values(package_id=package_id, tag="latest", version=latest_version, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["package_id", "tag"],
        set_={"version": latest_version, "updated_at": now},
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


async def _resolve_level1(db: AsyncSession, version_ids: Iterable[str]) -> None:
    dep_rows = (
        await db.scalars(select(Dependency).where(Dependency.version_id.in_(list(version_ids))))
    ).all()

    for dep in dep_rows:
        candidate_pkg = await db.scalar(
            select(Package).where(
                Package.ecosystem == Ecosystem.pypi,
                Package.normalized_name == normalize_package_name(dep.dependency_name),
            )
        )
        dep.resolved_package_id = candidate_pkg.id if candidate_pkg else None

        if not candidate_pkg:
            dep.resolved_version_id = None
            continue

        dep.resolved_version_id = await _find_best_version_id(db, candidate_pkg.id, dep.version_constraint)


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
