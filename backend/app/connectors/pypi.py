import asyncio
import logging
from datetime import UTC, datetime

import httpx
from packaging.version import InvalidVersion, Version

from app.config import get_settings
from app.connectors.base import (
    BaseConnector,
    NormalizedArtifact,
    NormalizedAuthor,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    NormalizedVulnerability,
    PackageNotFound,
)
from app.fetcher.transport import RegistryAuthError, RegistryClient, RegistryInfo
from app.normalizers.license import normalize_license
from app.normalizers.names import normalize_package_name
from app.normalizers.pep508 import parse_pep508
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)


def default_pypi_registry_info() -> RegistryInfo:
    settings = get_settings()
    return RegistryInfo(
        slug="pypi",
        ecosystem="pypi",
        protocol="pypi-json",
        base_url="https://pypi.org/",
        api_url=settings.sync_pypi_json_base_url_normalized,
    )


class PyPIConnector(BaseConnector):
    ecosystem = "pypi"
    protocol = "pypi-json"

    def __init__(self, timeout_seconds: float | None = None, registry: RegistryInfo | None = None) -> None:
        settings = get_settings()
        self.registry = registry or default_pypi_registry_info()
        self.json_base_url = (self.registry.api_url or self.registry.base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.sync_http_timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds
        self.client = RegistryClient(
            registry=self.registry,
            min_interval_seconds=self.get_rate_limit(),
            timeout_seconds=self.timeout_seconds,
        )

    async def fetch_package_metadata(self, name: str) -> NormalizedPackage:
        url = f"{self.json_base_url}/{name}/json"
        response = await self._fetch_with_retry(url, name)

        payload = response.json()
        info = payload.get("info", {})
        releases = payload.get("releases", {})

        package_name = info.get("name") or name
        normalized_name = normalize_package_name(package_name)
        classifiers = info.get("classifiers") or []
        license_spdx = normalize_license(info.get("license_expression"), classifiers, info.get("license"))

        latest_version = info.get("version") or _pick_latest_version(releases)
        latest_dependencies = _parse_dependencies(info.get("requires_dist") or [])
        versions = self._normalize_versions(
            releases, latest_version, latest_dependencies, info, payload.get("urls") or []
        )

        versions_sorted = sorted(versions, key=lambda v: v.release_date or datetime.min.replace(tzinfo=UTC), reverse=True)
        created_at = versions_sorted[-1].release_date if versions_sorted and versions_sorted[-1].release_date else datetime.now(UTC)
        updated_at = versions_sorted[0].release_date if versions_sorted and versions_sorted[0].release_date else datetime.now(UTC)

        vulnerabilities = self._normalize_vulnerabilities(payload.get("vulnerabilities") or [])
        authors = self._normalize_authors(info)

        keywords = info.get("keywords")
        keyword_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None

        return NormalizedPackage(
            ecosystem="pypi",
            name=package_name,
            normalized_name=normalized_name,
            description=info.get("summary") or info.get("description"),
            homepage_url=info.get("home_page"),
            repository_url=info.get("project_urls", {}).get("Source") if info.get("project_urls") else None,
            documentation_url=info.get("docs_url"),
            license_raw=info.get("license"),
            license_spdx=license_spdx,
            keywords=keyword_list,
            latest_version=latest_version,
            downloads_total=None,
            downloads_recent=None,
            created_at=created_at,
            updated_at=updated_at,
            raw_metadata={"info": info, "urls": payload.get("urls") or []},
            versions=versions_sorted,
            vulnerabilities=vulnerabilities,
            authors=authors,
        )

    async def fetch_version_dependencies(self, name: str, version: str) -> tuple[list[NormalizedDependency], str | None]:
        url = f"{self.json_base_url}/{name}/{version}/json"
        response = await self._fetch_with_retry(url, f"{name}=={version}")
        payload = response.json()
        info = payload.get("info", {})
        return _parse_dependencies(info.get("requires_dist") or []), info.get("requires_python")

    async def fetch_version_metadata(self, name: str, version: str) -> NormalizedVersion:
        url = f"{self.json_base_url}/{name}/{version}/json"
        response = await self._fetch_with_retry(url, f"{name}=={version}")
        payload = response.json()
        info = payload.get("info", {})
        urls = payload.get("urls") or []
        return _normalize_single_version(version, info, urls)

    async def test_connection(self) -> tuple[bool, str]:
        url = f"{self.json_base_url}/pip/json"
        try:
            response = await self.client.get(url)
        except RegistryAuthError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"{exc.__class__.__name__}: {exc}"
        if response.status_code >= 500:
            return False, f"upstream status {response.status_code}"
        return True, f"status {response.status_code}"

    async def _fetch_with_retry(self, url: str, package_name: str) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    raise PackageNotFound(package_name)
                response.raise_for_status()
                return response
            except (PackageNotFound, RegistryAuthError):
                raise
            except Exception as exc:  # noqa: BLE001
                is_retryable = is_retryable_upstream_error(exc)
                if (not is_retryable) or attempt > (self.max_retries + 1):
                    raise
                delay = compute_backoff_delay_seconds(
                    attempt=attempt,
                    base_seconds=self.retry_base_seconds,
                    max_seconds=self.retry_max_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                logger.warning(
                    "PyPI request retry: package=%s attempt=%s/%s delay=%.2fs error=%s",
                    package_name,
                    attempt,
                    self.max_retries + 1,
                    delay,
                    exc.__class__.__name__,
                )
                await asyncio.sleep(delay)

    def _normalize_versions(
        self,
        releases: dict,
        latest_version: str,
        latest_dependencies: list[NormalizedDependency],
        info: dict,
        urls: list,
    ) -> list[NormalizedVersion]:
        versions: list[NormalizedVersion] = []

        for version, files in releases.items():
            if not files:
                continue

            release_date = _max_upload_time(files)
            status = "yanked" if any(f.get("yanked") for f in files) else "active"
            # Только latest-версия несёт классификатору свой info (JSON API отдаёт
            # info пакета == info latest); историческим версиям info дозагружается
            # лениво через fetch_version_metadata при version-scoped-запросе.
            raw_metadata = {"info": info, "urls": urls} if version == latest_version else {"files": files}

            versions.append(
                NormalizedVersion(
                    version=version,
                    release_date=release_date,
                    status=status,
                    requires_python=files[0].get("requires_python") if files else None,
                    raw_metadata=raw_metadata,
                    dependencies_hydrated=version == latest_version,
                    artifacts=_artifacts_from_files(files),
                    dependencies=latest_dependencies if version == latest_version else [],
                )
            )

        return versions

    def _normalize_vulnerabilities(self, payload: list[dict]) -> list[NormalizedVulnerability]:
        vulnerabilities: list[NormalizedVulnerability] = []
        for item in payload:
            source_id = str(item.get("id") or "unknown")
            raw_summary = item.get("summary") or item.get("details") or f"Advisory {source_id}"
            summary = str(raw_summary).strip() or f"Advisory {source_id}"
            vulnerabilities.append(
                NormalizedVulnerability(
                    source="pypi_native",
                    source_id=source_id,
                    aliases=item.get("aliases") or [],
                    summary=summary,
                    details=item.get("details"),
                    severity=item.get("severity"),
                    fixed_in=item.get("fixed_in"),
                    published_at=_parse_datetime(item.get("published")),
                    withdrawn_at=_parse_datetime(item.get("withdrawn")),
                    link=item.get("link") or "https://pypi.org/",
                )
            )
        return vulnerabilities

    def _normalize_authors(self, info: dict) -> list[NormalizedAuthor]:
        authors: list[NormalizedAuthor] = []
        if info.get("author"):
            authors.append(
                NormalizedAuthor(
                    name=info.get("author"),
                    email=info.get("author_email"),
                    url=None,
                    role="author",
                )
            )
        if info.get("maintainer"):
            authors.append(
                NormalizedAuthor(
                    name=info.get("maintainer"),
                    email=info.get("maintainer_email"),
                    url=None,
                    role="maintainer",
                )
            )
        return authors

    def get_rate_limit(self) -> float:
        return 0.0


def _pick_latest_version(releases: dict) -> str:
    if not releases:
        return "0"
    parsed: list[tuple[Version, str]] = []
    for raw in releases:
        try:
            parsed.append((Version(raw), raw))
        except InvalidVersion:
            continue
    if not parsed:
        return sorted(releases.keys())[-1]
    stable = [(version, raw) for version, raw in parsed if not version.is_prerelease]
    pool = stable or parsed
    return max(pool, key=lambda item: item[0])[1]


def _artifacts_from_files(files: list) -> list[NormalizedArtifact]:
    artifacts: list[NormalizedArtifact] = []
    for file_payload in files:
        digests = file_payload.get("digests") or {}
        artifacts.append(
            NormalizedArtifact(
                filename=file_payload.get("filename", ""),
                download_url=file_payload.get("url", ""),
                size_bytes=file_payload.get("size"),
                checksum_sha256=digests.get("sha256"),
                checksum_md5=digests.get("md5"),
                artifact_type="wheel" if (file_payload.get("packagetype") == "bdist_wheel") else "sdist",
                platform_tags=None,
                requires_dist_metadata=bool(file_payload.get("dist_info_metadata")),
            )
        )
    return artifacts


def _max_upload_time(files: list):
    latest = None
    for file_payload in files:
        dt = _parse_datetime(file_payload.get("upload_time_iso_8601"))
        if dt and (latest is None or dt > latest):
            latest = dt
    return latest


def _normalize_single_version(version: str, info: dict, urls: list) -> NormalizedVersion:
    """One pypi version from {name}/{version}/json — the version-scoped manifest
    the classifier needs (info + urls of exactly this version)."""
    yanked = bool(info.get("yanked")) or any(u.get("yanked") for u in urls)
    return NormalizedVersion(
        version=version,
        release_date=_max_upload_time(urls),
        status="yanked" if yanked else "active",
        requires_python=info.get("requires_python"),
        raw_metadata={"info": info, "urls": urls},
        dependencies_hydrated=True,
        artifacts=_artifacts_from_files(urls),
        dependencies=_parse_dependencies(info.get("requires_dist") or []),
    )


def _parse_dependencies(raw_requirements: list[str]) -> list[NormalizedDependency]:
    parsed: list[NormalizedDependency] = []
    for req in raw_requirements:
        try:
            dep = parse_pep508(req)
        except ValueError:
            continue
        parsed.append(
            NormalizedDependency(
                dependency_name=dep.dependency_name,
                version_constraint=dep.version_constraint,
                dependency_type=dep.dependency_type,
                extra_name=dep.extra_name,
                environment_markers=dep.environment_markers,
                raw_spec=dep.raw_spec,
            )
        )
    return parsed


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
