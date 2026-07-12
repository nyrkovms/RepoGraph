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
