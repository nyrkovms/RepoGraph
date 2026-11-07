from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PackageListItem(BaseModel):
    ecosystem: str
    namespace: str
    name: str
    normalized_name: str
    purl: str
    origin: str
    description: str | None
    license_spdx: str | None
    latest_version: str | None
    updated_at: datetime
    has_vulnerabilities: bool
    used_by: int = 0                       # catalog packages that depend on it, see services/usage.used_by_counts


class PackageListResponse(BaseModel):
    data: list[PackageListItem]
    cursor: str | None
    has_more: bool


class PackageSourceItem(BaseModel):
    registry: str
    enabled: bool
    primary: bool
    first_seen_at: datetime
    checked_at: datetime | None
    missing_since: datetime | None


class PackageDetailResponse(BaseModel):
    ecosystem: str
    name: str
    normalized_name: str
    namespace: str = ""
    purl: str
    origin: str
    description: str | None
    homepage_url: str | None
    repository_url: str | None
    documentation_url: str | None
    license_raw: str | None
    license_spdx: str | None
    latest_version: str | None
    synced_at: datetime
    unavailable_at: datetime | None = None
    deprecation: dict | None = None        # {"message": ...}: npm deprecated, Go `// Deprecated:`
    sources: list[PackageSourceItem] = Field(default_factory=list)


class VersionItem(BaseModel):
    version: str
    release_date: datetime | None
    status: str
    status_reason: str | None = None       # why yanked: Go retract rationale, PyPI yanked reason
    deprecation: dict | None = None        # npm deprecated, NuGet deprecation (reasons, message, alternate)
    requires_python: str | None


class VersionListResponse(BaseModel):
    data: list[VersionItem]
    cursor: str | None
    has_more: bool


class VersionSourceItem(BaseModel):
    registry: str
    enabled: bool
    status: str
    first_seen_at: datetime


class VersionDetailResponse(BaseModel):
    name: str
    purl: str
    version: str
    release_date: datetime | None
    status: str
    status_reason: str | None = None
    deprecation: dict | None = None
    requires_python: str | None
    engines: dict | None = None
    features: dict | None
    dependencies_state: str
    sources: list[VersionSourceItem] = Field(default_factory=list)


class DependencyNode(BaseModel):
    name: str
    constraint: str
    dependency_type: str
    extra_name: str | None
    scope: str | None = None               # the ecosystem's own scope: Maven compile/runtime/test/provided/system
    environment_markers: dict | None = None
    details: dict | None = None            # Go `indirect`, Cargo features and registry, npm peer `optional`
    resolved_version: str | None
    resolved_name: str | None = None       # the catalog package the edge leads to when the name is not it (RPM capability)
    cycle: bool = False
    dependencies_unknown: bool = False   # the resolved version's source has no metadata for it (Q14)
    children: list[DependencyNode] = Field(default_factory=list)


DependencyNode.model_rebuild()


class DependencyTreeResponse(BaseModel):
    package: str
    version: str
    profile: str
    depth: int
    dependencies_unknown: bool = False
    dependencies: list[DependencyNode]


class ArtifactItem(BaseModel):
    filename: str
    artifact_type: str
    download_url: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    checksum_md5: str | None
    registry: str | None = None
    arch: str | None = None                # RPM: the build's arch (x86_64, noarch, ...)


class ArtifactListResponse(BaseModel):
    data: list[ArtifactItem]


class VulnerabilityItem(BaseModel):
    source: str
    source_id: str
    summary: str
    severity: str | None
    fixed_in: list[str] | None
    link: str


class VulnerabilityListResponse(BaseModel):
    package: str
    # `not_collected`: no source feeds advisories for the ecosystem, so an empty `data` says nothing about safety.
    coverage: Literal["collected", "not_collected"]
    data: list[VulnerabilityItem]


class OverviewDependencies(BaseModel):
    """Distinct names of the viewed version; every count is null while its dependencies are unavailable."""

    direct: int | None
    optional: int | None
    dev: int | None
    indirect: int | None                   # a lower bound: counted over the catalog's latest versions
    not_in_catalog: int | None


class OverviewUsedBy(BaseModel):
    direct: int
    indirect: int


class OverviewLicense(BaseModel):
    license: str
    count: int


class OverviewVersions(BaseModel):
    total: int
    prereleases: int
    not_active: int


class OverviewMonth(BaseModel):
    month: str                             # YYYY-MM, UTC
    releases: int


class OverviewLine(BaseModel):
    line: str                              # 2.14 (Go: v1)
    latest: str
    released_at: datetime
    stable_releases: int
    started_at: datetime


class PackageOverviewResponse(BaseModel):
    version: str | None                    # null for a package with no versions
    dependencies: OverviewDependencies
    used_by: OverviewUsedBy
    licenses: list[OverviewLicense]
    versions: OverviewVersions
    activity: list[OverviewMonth]
    lines: list[OverviewLine]


class EcosystemCount(BaseModel):
    ecosystem: str
    packages: int


class LicenseCount(BaseModel):
    license: str
    count: int


class LatestReleaseItem(PackageListItem):
    released_at: datetime


class StaleInUse(BaseModel):
    packages: int
    years: int
    min_used_by: int
    released_before: datetime


class CatalogSummaryResponse(BaseModel):
    ecosystems: list[EcosystemCount]
    licenses: list[LicenseCount]
    most_used: list[PackageListItem]
    latest_releases: list[LatestReleaseItem]
    deprecated_in_use: list[PackageListItem]
    stale_in_use: StaleInUse
