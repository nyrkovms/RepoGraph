"""Source adapters of the PyPI plugin. `pypi-warehouse` (pypi.org) wraps the existing connector and converts its
output into the contract types; `pypi-simple` (PEP 503/691 indexes) comes in E0.5.
"""

from collections.abc import Sequence
from typing import cast

from packaging.utils import canonicalize_name

from app.connectors import base as legacy
from app.connectors.base import PackageNotFound
from app.ecosystems.base import (
    ChangeDetector,
    DependencyType,
    NormalizedArtifact,
    NormalizedAuthor,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    NormalizedVulnerability,
    PackageRef,
    RegistryConfig,
    SourceLookup,
    VersionDependencies,
    VersionStatus,
)
from app.ecosystems.pypi.changes import WarehouseChangeDetector
from app.ecosystems.pypi.connector import PyPIConnector


def _dependency(dep: legacy.NormalizedDependency) -> NormalizedDependency:
    return NormalizedDependency(
        target=PackageRef(ecosystem="pypi", namespace="", name=canonicalize_name(dep.dependency_name)),
        constraint=dep.version_constraint,
        dependency_type=cast(DependencyType, dep.dependency_type),
        raw_spec=dep.raw_spec,
        extra_name=dep.extra_name,
        environment_markers=dep.environment_markers,
    )


def _artifact(artifact: legacy.NormalizedArtifact) -> NormalizedArtifact:
    checksums = {"sha256": artifact.checksum_sha256, "md5": artifact.checksum_md5}
    return NormalizedArtifact(
        filename=artifact.filename,
        artifact_type=artifact.artifact_type,
        download_url=artifact.download_url,
        checksums={kind: value for kind, value in checksums.items() if value},
        size_bytes=artifact.size_bytes,
        platform_tags=artifact.platform_tags,
        has_metadata_file=artifact.requires_dist_metadata,
    )


def _version(version: legacy.NormalizedVersion) -> NormalizedVersion:
    return NormalizedVersion(
        version=version.version,
        status=cast(VersionStatus, version.status),
        release_date=version.release_date,
        status_reason=version.yanked_reason,
        engines={"python": version.requires_python} if version.requires_python else None,
        raw_metadata=version.raw_metadata,
        artifacts=[_artifact(a) for a in version.artifacts],
        # The connector only carries dependencies for hydrated versions; None keeps the others lazy.
        dependencies=[_dependency(d) for d in version.dependencies] if version.dependencies_hydrated else None,
    )


def to_contract(package: legacy.NormalizedPackage, registry_id: str) -> NormalizedPackage:
    return NormalizedPackage(
        ref=PackageRef(ecosystem="pypi", namespace="", name=package.normalized_name),
        registry_id=registry_id,
        display_name=package.name,
        revision=str(package.last_serial) if package.last_serial is not None else None,
        versions=[_version(v) for v in package.versions],
        description=package.description,
        homepage_url=package.homepage_url,
        repository_url=package.repository_url,
        documentation_url=package.documentation_url,
        license_raw=package.license_raw,
        license_spdx=package.license_spdx,
        keywords=package.keywords,
        dist_tags={"latest": package.latest_version} if package.latest_version else None,
        downloads_total=package.downloads_total,
        downloads_recent=package.downloads_recent,
        created_at=package.created_at,
        updated_at=package.updated_at,
        authors=[NormalizedAuthor(name=a.name, email=a.email, url=a.url, role=a.role) for a in package.authors],
        vulnerabilities=[
            NormalizedVulnerability(
                source=v.source, source_id=v.source_id, aliases=v.aliases, summary=v.summary, details=v.details,
                severity=v.severity, fixed_in=v.fixed_in, published_at=v.published_at, withdrawn_at=v.withdrawn_at,
                link=v.link,
            )
            for v in package.vulnerabilities
        ],
        raw_metadata=package.raw_metadata,
    )


class WarehouseAdapter:
    """pypi.org: JSON API for metadata, Simple index serials for change detection."""

    def __init__(self, registry: RegistryConfig, connector: PyPIConnector | None = None) -> None:
        self.registry = registry
        self.connector = connector or PyPIConnector()

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        try:
            package = await self.connector.fetch_package_metadata(ref.name)
        except PackageNotFound:
            return None
        return to_contract(package, self.registry.id)

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies:
        # PackageNotFound propagates as today: a missing version is not "metadata unavailable".
        dependencies, requires_python = await self.connector.fetch_version_dependencies(ref.name, version)
        return VersionDependencies(
            dependencies=[_dependency(d) for d in dependencies],
            engines={"python": requires_python} if requires_python else None,
        )

    async def exists(self, ref: PackageRef) -> bool:
        return await self.fetch_package(ref, _NO_LOOKUP) is not None

    async def test_connection(self) -> None:
        # The JSON API has no root document; a project every index carries stands in for it.
        await self.exists(PackageRef(ecosystem="pypi", namespace="", name="pip"))

    def change_detector(self) -> ChangeDetector:
        return WarehouseChangeDetector()

    def min_interval_seconds(self) -> float:
        return self.connector.get_rate_limit()


class _NoLookup:
    def adapters_for(self, ref: PackageRef) -> Sequence["WarehouseAdapter"]:
        return ()


_NO_LOOKUP: SourceLookup = _NoLookup()
