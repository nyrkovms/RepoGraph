from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


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
    line: str | None = None                # release line (2.14, v1); None where the ecosystem has none
    is_prerelease: bool = False


class VersionListResponse(BaseModel):
    data: list[VersionItem]
    cursor: str | None
    has_more: bool


class CompareVersion(BaseModel):
    version: str
    release_date: datetime | None
    status: str


class CompareDependency(BaseModel):
    name: str
    constraint: str
    dependency_type: str


class CompareChange(BaseModel):
    name: str
    dependency_type: str
    from_: str = Field(serialization_alias="from")
    to: str

    model_config = ConfigDict(populate_by_name=True)


class CompareDependencies(BaseModel):
    added: list[CompareDependency]
    removed: list[CompareDependency]
    changed: list[CompareChange]
    unchanged: int


class VersionCompareResponse(BaseModel):
    from_: CompareVersion | None = Field(serialization_alias="from")
    to: CompareVersion
    dependencies: CompareDependencies
    dependencies_unknown: bool

    model_config = ConfigDict(populate_by_name=True)


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
    line: str | None = None                # release line, as in the versions list
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
    license: str | None = None             # of the resolved package: SPDX id, else the raw string, else null
    cycle: bool = False
    dependencies_unknown: bool = False   # the resolved version's source has no metadata for it (Q14)
    children: list[DependencyNode] = Field(default_factory=list)
    _package_id: uuid.UUID | None = PrivateAttr(default=None)   # the catalog package of the node; licenses are loaded by it


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
    indirect: int | None                   # a lower bound: counted over the catalog's latest versions; null for RPM
    not_in_catalog: int | None             # null for RPM


class OverviewUsedBy(BaseModel):
    direct: int
    indirect: int | None                   # null for RPM: a capability's package is not resolved here, see the overview route


class DependentItem(BaseModel):
    ecosystem: str
    namespace: str
    name: str
    normalized_name: str                   # with `namespace`, the native spelling the card links by
    purl: str
    latest_version: str | None
    relation: Literal["direct", "indirect"]
    dependency_types: list[str]            # of the direct edges; empty for an indirect dependent
    requires: list[str]                    # distinct constraints it puts on this package; empty for an indirect one


class DependentTotals(BaseModel):
    direct: int
    indirect: int


class DependentListResponse(BaseModel):
    totals: DependentTotals                # over the whole list, whatever `relation` filters
    data: list[DependentItem]
    cursor: str | None
    has_more: bool


class OverviewLicense(BaseModel):
    license: str
    count: int


class OverviewVersions(BaseModel):
    total: int
    prereleases: int
    not_active: int                        # by effective status (yanked, unlisted, ...); `deprecated` is counted apart
    deprecated: int                        # versions carrying a deprecation (npm, NuGet keep it with status `active`)


class OverviewMonth(BaseModel):
    month: str                             # YYYY-MM, UTC
    releases: int


class OverviewLine(BaseModel):
    line: str                              # 2.14 (Go: v1)
    latest: str
    released_at: datetime | None           # of `latest`; null if the source gives it no date
    stable_releases: int
    started_at: datetime | None            # the earliest dated release; null if none has a date


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
