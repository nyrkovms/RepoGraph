from enum import Enum


class Ecosystem(str, Enum):
    pypi = "pypi"
    npm = "npm"
    maven = "maven"
    go = "go"
    crates = "crates"
    nuget = "nuget"


class RegistryType(str, Enum):
    public = "public"
    corporate = "corporate"
    mirror = "mirror"


class Tier(str, Enum):
    tier1 = "tier1"
    tier2 = "tier2"
    tier3 = "tier3"


class PackageVersionStatus(str, Enum):
    active = "active"
    yanked = "yanked"
    deprecated = "deprecated"
    unlisted = "unlisted"
    removed = "removed"


class DependencyType(str, Enum):
    runtime = "runtime"
    dev = "dev"
    optional = "optional"
    build = "build"
    peer = "peer"
    provided = "provided"


class VulnerabilitySource(str, Enum):
    osv = "osv"
    pypi_native = "pypi_native"


class VulnerabilitySeverity(str, Enum):
    unknown = "unknown"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class SyncType(str, Enum):
    full = "full"
    incremental = "incremental"


class SyncStatus(str, Enum):
    idle = "idle"
    running = "running"
    failed = "failed"


class PackageEventKind(str, Enum):
    tracking_started = "tracking_started"
    release_added = "release_added"
    release_yanked = "release_yanked"
    release_unyanked = "release_unyanked"
    release_removed = "release_removed"
    release_restored = "release_restored"
    vulnerability_added = "vulnerability_added"
    vulnerability_withdrawn = "vulnerability_withdrawn"
    project_unavailable = "project_unavailable"
    project_restored = "project_restored"


class UpdateRunStatus(str, Enum):
    running = "running"
    success = "success"
    partial = "partial"
    failed = "failed"
