"""npm connector: registry.npmjs.org protocol (also Verdaccio / Nexus npm hosted).

Package metadata comes from the full packument (all versions, dist-tags,
deprecations, maintainers); the heavyweight `versions` map is stripped from
the stored package raw_metadata — each version keeps its own manifest.
Historical-version dependencies hydrate lazily via the versioned endpoint.
"""

import asyncio
import logging
from datetime import UTC, datetime
from urllib.parse import quote

from app.config import get_settings
from app.connectors.base import (
    BaseConnector,
    NormalizedArtifact,
    NormalizedAuthor,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    PackageNotFound,
)
from app.fetcher.transport import RegistryAuthError, RegistryClient, RegistryInfo
from app.normalizers.license import normalize_license
from app.normalizers.names import normalize_name, npm_scope
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

DEPENDENCY_FIELDS = (
    ("dependencies", "runtime"),
    ("devDependencies", "dev"),
    ("peerDependencies", "peer"),
    ("optionalDependencies", "optional"),
)


def encode_npm_name(name: str) -> str:
    """Scoped names keep '@' but escape the slash: @types/node -> @types%2Fnode."""
    return quote(name, safe="@")


def parse_manifest_dependencies(manifest: dict) -> list[NormalizedDependency]:
    dependencies: list[NormalizedDependency] = []
    optional_names = set((manifest.get("optionalDependencies") or {}).keys())
    for field_name, dep_type in DEPENDENCY_FIELDS:
        for dep_name, constraint in (manifest.get(field_name) or {}).items():
            # npm duplicates optional deps inside `dependencies`; keep the optional row only.
            if field_name == "dependencies" and dep_name in optional_names:
                continue
            dependencies.append(
                NormalizedDependency(
                    dependency_name=dep_name,
                    version_constraint=str(constraint) if constraint else "*",
                    dependency_type=dep_type,
                    extra_name=None,
                    environment_markers=None,
                    raw_spec=f"{dep_name}@{constraint}",
                )
            )
    return dependencies


class NpmConnector(BaseConnector):
    ecosystem = "npm"
    protocol = "npm"

    def __init__(self, registry: RegistryInfo, timeout_seconds: float | None = None) -> None:
        settings = get_settings()
        self.registry = registry
        self.base_url = (registry.api_url or registry.base_url).rstrip("/")
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds
        self.client = RegistryClient(
            registry=registry,
            min_interval_seconds=self.get_rate_limit(),
            timeout_seconds=timeout_seconds or settings.sync_http_timeout_seconds,
        )

    def get_rate_limit(self) -> float:
        return 0.0

    async def _get_json_with_retry(self, url: str, *, not_found_name: str) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    raise PackageNotFound(not_found_name)
                response.raise_for_status()
                return response.json()
            except (PackageNotFound, RegistryAuthError):
                raise
            except Exception as exc:  # noqa: BLE001
                if (not is_retryable_upstream_error(exc)) or attempt > (self.max_retries + 1):
                    raise
                delay = compute_backoff_delay_seconds(
                    attempt=attempt,
                    base_seconds=self.retry_base_seconds,
                    max_seconds=self.retry_max_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                logger.warning(
                    "npm request retry: registry=%s url=%s attempt=%s error=%s",
                    self.registry.slug,
                    url,
                    attempt,
                    exc.__class__.__name__,
                )
                await asyncio.sleep(delay)

    async def fetch_package_metadata(self, name: str) -> NormalizedPackage:
        normalized = normalize_name("npm", name)
        packument = await self._get_json_with_retry(
            f"{self.base_url}/{encode_npm_name(normalized)}", not_found_name=name
        )

        dist_tags = {str(tag): str(version) for tag, version in (packument.get("dist-tags") or {}).items()}
        latest_version = dist_tags.get("latest") or ""
        time_map = packument.get("time") or {}
        versions_map = packument.get("versions") or {}
        if not latest_version and versions_map:
            latest_version = sorted(versions_map)[-1]

        versions = [
            self._normalize_version(manifest, time_map, hydrate_deps=(version == latest_version))
            for version, manifest in versions_map.items()
        ]
        versions.sort(key=lambda v: v.release_date or datetime.min.replace(tzinfo=UTC), reverse=True)

        latest_manifest = versions_map.get(latest_version) or {}
        license_raw = _license_string(packument.get("license") or latest_manifest.get("license"))
        keywords = packument.get("keywords") or latest_manifest.get("keywords") or None

        deprecation_message = latest_manifest.get("deprecated")
        raw_metadata = {k: v for k, v in packument.items() if k not in ("versions", "readme")}

        return NormalizedPackage(
            ecosystem="npm",
            name=packument.get("name") or normalized,
            normalized_name=normalized,
            description=packument.get("description") or latest_manifest.get("description"),
            homepage_url=packument.get("homepage") or latest_manifest.get("homepage"),
            repository_url=_repository_url(packument.get("repository") or latest_manifest.get("repository")),
            documentation_url=None,
            license_raw=license_raw,
            license_spdx=normalize_license(None, [], license_raw),
            keywords=keywords if isinstance(keywords, list) else None,
            latest_version=latest_version,
            downloads_total=None,
            downloads_recent=None,
            created_at=_parse_datetime(time_map.get("created")) or datetime.now(UTC),
            updated_at=_parse_datetime(time_map.get("modified")) or datetime.now(UTC),
            raw_metadata=raw_metadata,
            versions=versions,
            vulnerabilities=[],
            authors=self._normalize_authors(packument),
            completeness="full",
            namespace=npm_scope(normalized),
            dist_tags=dist_tags,
            deprecated=bool(deprecation_message),
            deprecation_message=str(deprecation_message) if deprecation_message else None,
        )

    def _normalize_version(self, manifest: dict, time_map: dict, *, hydrate_deps: bool) -> NormalizedVersion:
        version = str(manifest.get("version") or "")
        dist = manifest.get("dist") or {}
        tarball_url = str(dist.get("tarball") or "")
        deprecated = manifest.get("deprecated")

        artifacts: list[NormalizedArtifact] = []
        if tarball_url:
            artifacts.append(
                NormalizedArtifact(
                    filename=tarball_url.rsplit("/", 1)[-1],
                    download_url=tarball_url,
                    size_bytes=None,
                    checksum_sha256=None,
                    checksum_md5=None,
                    artifact_type="tgz",
                    platform_tags=_platform_tags(manifest, dist),
                    checksum_sha1=dist.get("shasum"),
                    checksum_integrity=dist.get("integrity"),
                )
            )

        return NormalizedVersion(
            version=version,
            release_date=_parse_datetime(time_map.get(version)),
            status="deprecated" if deprecated else "active",
            requires_python=None,
            raw_metadata=manifest,
            dependencies_hydrated=hydrate_deps,
            artifacts=artifacts,
            dependencies=parse_manifest_dependencies(manifest) if hydrate_deps else [],
            deprecation_message=str(deprecated) if deprecated else None,
        )

    async def fetch_version_dependencies(self, name: str, version: str) -> tuple[list[NormalizedDependency], str | None]:
        normalized = normalize_name("npm", name)
        manifest = await self._get_json_with_retry(
            f"{self.base_url}/{encode_npm_name(normalized)}/{quote(version)}",
            not_found_name=f"{name}@{version}",
        )
        return parse_manifest_dependencies(manifest), None

    @staticmethod
    def _normalize_authors(packument: dict) -> list[NormalizedAuthor]:
        authors: list[NormalizedAuthor] = []
        author = packument.get("author")
        if isinstance(author, dict) and author.get("name"):
            authors.append(NormalizedAuthor(name=author["name"], email=author.get("email"), url=author.get("url"), role="author"))
        elif isinstance(author, str) and author:
            authors.append(NormalizedAuthor(name=author, email=None, url=None, role="author"))
        for maintainer in packument.get("maintainers") or []:
            if isinstance(maintainer, dict) and maintainer.get("name"):
                authors.append(
                    NormalizedAuthor(name=maintainer["name"], email=maintainer.get("email"), url=None, role="maintainer")
                )
        return authors

    async def test_connection(self) -> tuple[bool, str]:
        try:
            response = await self.client.get(f"{self.base_url}/")
        except RegistryAuthError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"{exc.__class__.__name__}: {exc}"
        if response.status_code >= 500:
            return False, f"upstream status {response.status_code}"
        return True, f"status {response.status_code}"


def _platform_tags(manifest: dict, dist: dict) -> dict | None:
    tags: dict = {}
    if dist.get("unpackedSize") is not None:
        tags["unpacked_size"] = dist["unpackedSize"]
    if dist.get("fileCount") is not None:
        tags["file_count"] = dist["fileCount"]
    if manifest.get("os"):
        tags["os"] = manifest["os"]
    if manifest.get("cpu"):
        tags["cpu"] = manifest["cpu"]
    return tags or None


def _repository_url(repository) -> str | None:
    if isinstance(repository, dict):
        return repository.get("url")
    if isinstance(repository, str):
        return repository
    return None


def _license_string(license_field) -> str | None:
    if isinstance(license_field, str):
        return license_field
    if isinstance(license_field, dict):
        return license_field.get("type")
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
