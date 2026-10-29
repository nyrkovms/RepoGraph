"""Source adapters of the PyPI plugin. `pypi-warehouse` (pypi.org) wraps the existing connector and converts its
output into the contract types; `pypi-simple` (PEP 503/691 indexes) comes in E0.5.
"""

from collections.abc import Sequence
from typing import cast
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name

from app.config import get_settings
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
from app.ecosystems.pypi.simple_index import PyPISimpleIndex
from app.services.http import make_client


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


def warehouse_urls(registry: RegistryConfig) -> tuple[str | None, str | None]:
    """JSON API and Simple index of a warehouse source. pypi.org keeps the SYNC_PYPI_*_BASE_URL settings (None here),
    which README offers for a proxy in front of it; any other source is read from its own `base_url`, with
    `config.api_url` / `config.simple_url` for a non-standard layout."""
    if urlsplit(registry.base_url).hostname == "pypi.org":
        return None, None
    base = registry.base_url.rstrip("/")
    api_url, simple_url = registry.config.get("api_url"), registry.config.get("simple_url")
    return (
        api_url if isinstance(api_url, str) and api_url else f"{base}/pypi",
        simple_url if isinstance(simple_url, str) and simple_url else f"{base}/simple",
    )


class WarehouseAdapter:
    """pypi.org: JSON API for metadata, Simple index serials for change detection."""

    def __init__(self, registry: RegistryConfig, connector: PyPIConnector | None = None) -> None:
        self.registry = registry
        self.json_url, self.simple_url = warehouse_urls(registry)
        self.connector = connector or PyPIConnector(json_base_url=self.json_url)

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
        # The JSON API has no root document; a project every public index carries stands in for it.
        if await self.exists(PackageRef(ecosystem="pypi", namespace="", name="pip")):
            return
        # A private index may lack `pip`, but it serves its Simple root; a wrong base_url fails here.
        root = (self.simple_url or get_settings().sync_pypi_simple_base_url).rstrip("/") + "/"
        async with make_client(timeout=30.0) as client:
            response = await client.get(root, headers={"Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.1"})
        response.raise_for_status()

    def change_detector(self) -> ChangeDetector:
        return WarehouseChangeDetector(PyPISimpleIndex(base_url=self.simple_url))

    def min_interval_seconds(self) -> float:
        return self.connector.get_rate_limit()


class _NoLookup:
    def adapters_for(self, ref: PackageRef) -> Sequence["WarehouseAdapter"]:
        return ()


_NO_LOOKUP: SourceLookup = _NoLookup()
