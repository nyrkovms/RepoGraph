from __future__ import annotations

from datetime import datetime

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
    environment_markers: dict | None = None
    details: dict | None = None            # Go `indirect`, Cargo features and registry, npm peer `optional`
    resolved_version: str | None
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
    data: list[VulnerabilityItem]
