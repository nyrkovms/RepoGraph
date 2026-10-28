"""Ecosystem plugin contract (SPEC-MULTI-ECOSYSTEM.md §3, IMPL-MULTI-ECOSYSTEM.md A1).

Protected path: change only in a WP that says so. Mirrors design/ecosystem_contract.py.

Layers:
  EcosystemPlugin  — semantics of an ecosystem: names, versions, environment, routing, merge policy, resolver.
  SourceAdapter    — one per (protocol, registry): how to read a concrete source. pypi.org and the PyTorch
                     index are two adapters of the same plugin.
  ChangeDetector   — produced by an adapter: daily "what changed" for that source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

Ecosystem = Literal["pypi", "npm", "maven", "nuget", "go", "crates", "rpm"]
Protocol_ = Literal[
    "pypi-warehouse", "pypi-simple", "npm-registry", "maven2", "nuget-v3", "goproxy", "cargo-sparse", "rpm-repodata"
]
VersionStatus = Literal["active", "yanked", "unlisted", "removed", "superseded"]
DependencyType = Literal["runtime", "dev", "optional", "build", "peer", "provided"]
Bump = Literal["major", "minor", "patch", "pre", "other"]


# --- identity -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackageRef:
    """Normalized identity. `namespace` is '' when the ecosystem has none (matches packages.namespace)."""

    ecosystem: Ecosystem
    namespace: str
    name: str


class Coordinates(Protocol):
    def parse(self, raw: str) -> PackageRef:
        """Native notation: 'flask', '@corp/ui', 'org.springframework:spring-core', 'github.com/x/y/v2', 'rhel-9/openssl'."""
        ...

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]: ...

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str: ...

    def display(self, ref: PackageRef) -> str: ...


# --- versions -------------------------------------------------------------------------------------


class InvalidVersion(ValueError):
    pass


class Constraint(Protocol):
    def contains(self, version: str) -> bool: ...

    def raw(self) -> str: ...


class VersionScheme(Protocol):
    def validate(self, raw: str) -> None:
        """Raise InvalidVersion: e.g. npm rejects numeric parts above 2**53-1 and leading zeros in prerelease ids."""
        ...

    def sort_key(self, raw: str) -> bytes:
        """Byte order must equal the ecosystem order (verified per scheme against the native implementation)."""
        ...

    def is_prerelease(self, raw: str) -> bool: ...

    def bump(self, previous: str | None, current: str) -> Bump | None: ...

    def constraint(self, raw: str) -> Constraint:
        """PEP 440 specifier, node-semver range, Cargo req, Maven/NuGet interval, RPM flags+EVR. Go: minimum version."""
        ...


# --- sources --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    id: str
    slug: str
    ecosystem: Ecosystem
    protocol: Protocol_
    base_url: str
    priority: int
    config: Mapping[str, object]
    routing: Mapping[str, object] | None
    auth_type: Literal["none", "basic", "bearer", "tls_client_cert"]
    credentials_ref: str | None


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Ordered registries for discovery, tree and resolve (pip index list, Maven <repositories>, npm scope map)."""

    ecosystem: Ecosystem
    slug: str
    registries: Sequence[RegistryConfig]


@dataclass(slots=True)
class NormalizedDependency:
    target: PackageRef                      # RPM: namespace = distro, name = capability
    constraint: str
    dependency_type: DependencyType
    raw_spec: str
    scope: str | None = None
    alias: str | None = None
    extra_name: str | None = None
    environment_markers: Mapping[str, object] | None = None
    details: Mapping[str, object] | None = None   # exclusions, features, registry hint (Cargo `registry`, npm tarball URL)


@dataclass(slots=True)
class NormalizedArtifact:
    filename: str
    artifact_type: str
    download_url: str | None
    checksums: Mapping[str, str] = field(default_factory=dict)
    size_bytes: int | None = None
    arch: str | None = None
    platform_tags: Mapping[str, object] | None = None
    has_metadata_file: bool = False


@dataclass(slots=True)
class NormalizedVersion:
    version: str
    status: VersionStatus
    release_date: datetime | None = None
    status_reason: str | None = None
    deprecation: Mapping[str, object] | None = None
    engines: Mapping[str, str] | None = None
    features: Mapping[str, object] | None = None
    raw_metadata: Mapping[str, object] = field(default_factory=dict)
    artifacts: list[NormalizedArtifact] = field(default_factory=list)
    dependencies: list[NormalizedDependency] | None = None   # None = not loaded (lazy), [] = none


@dataclass(slots=True)
class NormalizedAuthor:
    name: str
    email: str | None
    url: str | None
    role: str


@dataclass(slots=True)
class NormalizedVulnerability:
    source: str
    source_id: str
    aliases: list[str]
    summary: str
    details: str | None
    severity: str | None
    fixed_in: list[str] | None
    published_at: datetime | None
    withdrawn_at: datetime | None
    link: str


@dataclass(slots=True)
class NormalizedPackage:
    """What ONE source says. Package-level fields are optional: a PEP 503 index knows only files."""

    ref: PackageRef
    registry_id: str
    display_name: str
    revision: str | None                   # serial / ETag / lastUpdated / EVR
    versions: list[NormalizedVersion]
    description: str | None = None
    homepage_url: str | None = None
    repository_url: str | None = None
    documentation_url: str | None = None
    license_raw: str | None = None
    license_spdx: str | None = None
    keywords: list[str] | None = None
    deprecation: Mapping[str, object] | None = None
    dist_tags: Mapping[str, str] | None = None
    downloads_total: int | None = None
    downloads_recent: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # None = this source does not provide the field; merge must not wipe what another source gave.
    authors: list[NormalizedAuthor] | None = None
    vulnerabilities: list[NormalizedVulnerability] | None = None   # kept for PyPI only (D6)
    raw_metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class VersionDependencies:
    dependencies: list[NormalizedDependency]
    engines: Mapping[str, str] | None = None
    available: bool = True                 # False: the source has no metadata for it (PEP 503 without PEP 658)


class SourceLookup(Protocol):
    """Lets an adapter fetch auxiliary documents from other sources of the same profile:
    a Maven parent POM or BOM may live in another repository than the artifact itself."""

    def adapters_for(self, ref: PackageRef) -> Sequence[SourceAdapter]: ...


class SourceAdapter(Protocol):
    registry: RegistryConfig

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        """None = the source does not have the package (404, absent from repodata)."""
        ...

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies: ...

    async def exists(self, ref: PackageRef) -> bool:
        """Cheap discovery probe; results are cached negatively in Redis."""
        ...

    def change_detector(self) -> ChangeDetector: ...

    def min_interval_seconds(self) -> float: ...


# --- daily update ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackedPackage:
    ref: PackageRef
    package_id: str
    revision: str | None                  # package_registries.upstream_revision for this source
    etag: str | None
    unavailable: bool


@dataclass(slots=True)
class ChangeSet:
    changed: list[tuple[TrackedPackage, str | None]]   # with the revision the source reports
    missing: list[TrackedPackage]
    cursor: Mapping[str, object]                       # new sync_states.cursor_data
    reconcile_all: bool = False                        # feed lost (npm seq reset): check every package conditionally


class ChangeDetector(Protocol):
    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet: ...


# --- environment, routing, merge, resolve ---------------------------------------------------------


class Environment(Protocol):
    """Typed per ecosystem: python_version/os; node/os/cpu; target_framework; target triple + features; distro/arch/modules."""

    def as_dict(self) -> Mapping[str, object]: ...


@dataclass(slots=True)
class ResolveRoot:
    ref: PackageRef
    constraint: str
    extras: Sequence[str] = ()             # PyPI extras, Cargo features
    qualifiers: Mapping[str, str] = field(default_factory=dict)   # purl qualifiers: Maven classifier/type, RPM arch


@dataclass(slots=True)
class ResolveRequest:
    roots: Sequence[ResolveRoot]
    environment: Environment
    profile: SourceProfile
    include_dev: bool = False
    include_optional: bool = False
    strategy: str | None = None            # maven: "maven" (nearest) | "gradle" (highest)
    managed: Sequence[ResolveRoot] = ()    # Maven dependencyManagement of the root
    timeout_s: float = 30.0
    max_nodes: int = 2000
    max_lazy_fetches: int = 500


@dataclass(slots=True)
class ResolvedNode:
    id: str
    ref: PackageRef
    version: str
    registry_slug: str                     # which source the chosen version came from
    depth: int
    errors: list[str] = field(default_factory=list)   # node-level problems: unknown dependencies, unresolvable requirement


@dataclass(slots=True)
class ResolvedEdge:
    source: str                            # node id or "root"
    target: str
    constraint: str
    dependency_type: DependencyType


@dataclass(slots=True)
class ResolveGraph:
    strategy: str
    nodes: list[ResolvedNode]
    edges: list[ResolvedEdge]
    conflicts: list[Mapping[str, object]]
    warnings: list[Mapping[str, object]]
    fetched: list[PackageRef]
    details: Mapping[str, object] = field(default_factory=dict)   # RPM: active module streams


class CatalogAccess(Protocol):
    """What resolvers and the tree need from the catalog: versions visible through given sources, lazy fetch."""

    async def versions(self, ref: PackageRef, registry_ids: Sequence[str]) -> Sequence[str]: ...

    async def dependencies(self, ref: PackageRef, version: str, registry_id: str) -> Sequence[NormalizedDependency] | None:
        """Dependencies as published by `registry_id`: its variant if the source disagrees with the canonical set.
        None = unknown (dependencies_state=unavailable)."""
        ...

    async def ensure(self, ref: PackageRef, profile: SourceProfile) -> bool:
        """Lazy fetch into the catalog with origin=discovered; False if no source has it."""
        ...


class Resolver(Protocol):
    emulates: str                          # native tool and version the behaviour follows: "npm 10", "Maven 3.9", "pip 24"

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph: ...


class EcosystemPlugin(Protocol):
    ecosystem: Ecosystem
    purl_type: str
    coordinates: Coordinates
    versions: VersionScheme

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        """Chooses the adapter by registry.protocol; unknown protocol is a configuration error."""
        ...

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        """Routing: profile order, filtered by rules (npm scopes, Maven group patterns, PyPI package pins).
        Rule modes: 'allow' (serve only matching packages), 'block' (never serve matching packages),
        'exclusive' (a match drops every other source), 'priority' (if the package exists here, other sources are
        ignored; Artifactory Priority Resolution). Probing for 'priority' happens in discovery, not here."""
        ...

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        """Several sources -> one catalog row: package fields from the highest-priority source that has them,
        a version's metadata from the highest-priority source publishing it, per-source statuses kept."""
        ...

    def parse_environment(self, raw: Mapping[str, object]) -> Environment: ...

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool: ...

    async def dependency_target(self, dep: NormalizedDependency, env: Environment, profile: SourceProfile) -> PackageRef | None:
        """Level-1 link for the tree. Identity for most ecosystems; RPM asks rpm-worker which package provides the capability."""
        ...

    def resolver(self) -> Resolver: ...


@dataclass(slots=True)
class MergedVersion:
    version: NormalizedVersion
    metadata_registry_id: str
    sources: Mapping[str, VersionStatus]   # registry_id -> status in that source (version_sources)
    dependency_variants: Mapping[str, list[NormalizedDependency]] = field(default_factory=dict)
    # registry_id -> dependencies, only for sources whose metadata differs from the canonical set


@dataclass(slots=True)
class MergedPackage:
    package: NormalizedPackage             # package-level fields after merge
    primary_registry_id: str
    versions: list[MergedVersion]
    registry_revisions: Mapping[str, str | None]   # registry_id -> revision (package_registries)
