from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class NormalizedDependency:
    dependency_name: str
    version_constraint: str
    dependency_type: str
    extra_name: str | None
    environment_markers: dict | None
    raw_spec: str


@dataclass(slots=True)
class NormalizedArtifact:
    filename: str
    download_url: str
    size_bytes: int | None
    checksum_sha256: str | None
    checksum_md5: str | None
    artifact_type: str
    platform_tags: dict | None
    requires_dist_metadata: bool = False


@dataclass(slots=True)
class NormalizedVersion:
    version: str
    release_date: datetime | None
    status: str
    requires_python: str | None
    raw_metadata: dict
    dependencies_hydrated: bool = False
    artifacts: list[NormalizedArtifact] = field(default_factory=list)
    dependencies: list[NormalizedDependency] = field(default_factory=list)


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
class NormalizedAuthor:
    name: str
    email: str | None
    url: str | None
    role: str


@dataclass(slots=True)
class NormalizedPackage:
    ecosystem: str
    name: str
    normalized_name: str
    description: str | None
    homepage_url: str | None
    repository_url: str | None
    documentation_url: str | None
    license_raw: str | None
    license_spdx: str | None
    keywords: list[str] | None
    latest_version: str
    downloads_total: int | None
    downloads_recent: int | None
    created_at: datetime
    updated_at: datetime
    raw_metadata: dict
    versions: list[NormalizedVersion] = field(default_factory=list)
    vulnerabilities: list[NormalizedVulnerability] = field(default_factory=list)
    authors: list[NormalizedAuthor] = field(default_factory=list)
    # "full" — протокол отдаёт полные метаданные (pypi-json, npm);
    # "minimal" — только имена/версии/файлы (pypi-simple): такие данные
    # не должны затирать поля, полученные из более богатого источника.
    completeness: str = "full"


class PackageNotFound(Exception):
    pass


class BaseConnector(ABC):
    ecosystem: str

    @abstractmethod
    async def fetch_package_metadata(self, name: str) -> NormalizedPackage:
        raise NotImplementedError

    @abstractmethod
    def get_rate_limit(self) -> float:
        raise NotImplementedError

    async def list_package_names(self) -> list[str]:
        """Full listing of package names for the enumeration loop.

        Supported by protocols with a listing endpoint (pypi-simple root,
        internal npm catalogs). Protocols without one raise.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support enumeration")

    async def test_connection(self) -> tuple[bool, str]:
        """Admin 'test connection': (ok, human-readable detail)."""
        raise NotImplementedError
