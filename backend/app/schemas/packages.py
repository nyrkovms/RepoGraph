from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PackageListItem(BaseModel):
    ecosystem: str
    name: str
    normalized_name: str
    description: str | None
    license_spdx: str | None
    latest_version: str
    updated_at: datetime
    has_vulnerabilities: bool


class PackageListResponse(BaseModel):
    data: list[PackageListItem]
    cursor: str | None
    has_more: bool


class FacetItem(BaseModel):
    role: str
    confidence: str
    evidence: str
    provisional: bool


class CapabilityItem(BaseModel):
    capability: str
    confidence: str
    evidence: str


class EffectiveCapabilityItem(BaseModel):
    name: str
    origin: str  # structural | user


class AnnotationItem(BaseModel):
    capability: str
    present: bool
    version: str
    note: str
    created_at: datetime


class RelationItem(BaseModel):
    relation: str
    name: str


class ClassificationBlock(BaseModel):
    classifier_version: str
    classified_at: datetime
    version: str
    status: str
    status_reason: str
    facets: list[FacetItem]
    capabilities: list[CapabilityItem]
    effective_capabilities: list[EffectiveCapabilityItem]
    annotations: list[AnnotationItem]
    relations: list[RelationItem]


class RegistryPresenceItem(BaseModel):
    slug: str
    registry_type: str
    is_primary: bool
    name_collision: bool
    checksum_mismatch: bool


class PackageDetailResponse(BaseModel):
    ecosystem: str
    name: str
    namespace: str | None = None
    normalized_name: str
    description: str | None
    homepage_url: str | None
    repository_url: str | None
    documentation_url: str | None
    license_raw: str | None
    license_spdx: str | None
    latest_version: str
    synced_at: datetime
    deprecated: bool = False
    deprecation_message: str | None = None
    classification: ClassificationBlock | None = None
    registries: list[RegistryPresenceItem] = []


class VersionItem(BaseModel):
    version: str
    release_date: datetime | None
    status: str
    requires_python: str | None


class VersionListResponse(BaseModel):
    data: list[VersionItem]
    cursor: str | None
    has_more: bool


class VersionDetailResponse(BaseModel):
    name: str
    version: str
    release_date: datetime | None
    status: str
    requires_python: str | None
    features: dict | None


class DependencyNode(BaseModel):
    name: str
    constraint: str
    dependency_type: str
    extra_name: str | None
    environment_markers: dict | None = None
    resolved_version: str | None
    cycle: bool = False
    children: list[DependencyNode] = Field(default_factory=list)


DependencyNode.model_rebuild()


class DependencyTreeResponse(BaseModel):
    package: str
    version: str
    depth: int
    dependencies: list[DependencyNode]


class ArtifactItem(BaseModel):
    filename: str
    artifact_type: str
    download_url: str
    size_bytes: int | None
    checksum_sha256: str | None
    checksum_md5: str | None


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
