"""pypi-simple connector: PEP 503 (HTML) / PEP 691 (JSON) simple index.

Serves registries without the pypi.org JSON API: the PyTorch index
(download.pytorch.org/whl), internal Nexus/Artifactory/devpi hosted repos.
Yields NormalizedPackage with completeness="minimal": names, versions, files,
hashes, requires-python, yanked — plus dependencies of the latest version when
PEP 658 core metadata is published. Minimal data never overwrites fields from
a richer protocol (merge rule lives in the sync pipeline).
"""

import asyncio
import logging
from datetime import UTC, datetime
from email.parser import Parser
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from app.config import get_settings
from app.connectors.base import (
    BaseConnector,
    NormalizedArtifact,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    PackageNotFound,
)
from app.fetcher.transport import RegistryAuthError, RegistryClient, RegistryInfo
from app.normalizers.names import normalize_package_name
from app.normalizers.pep508 import parse_pep508
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json, text/html;q=0.1"


class _AnchorCollector(HTMLParser):
    """Collects <a> tags: href + attrs + inner text (PEP 503 pages)."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._current = {"attrs": dict(attrs), "text": ""}

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self.anchors.append(self._current)
            self._current = None


def _parse_filename_version(filename: str) -> str | None:
    try:
        if filename.endswith(".whl"):
            return str(parse_wheel_filename(filename)[1])
        return str(parse_sdist_filename(filename)[1])
    except (InvalidWheelFilename, InvalidSdistFilename, ValueError):
        return None


def _pick_latest(versions: list[str]) -> str:
    parsed: list[tuple[Version, str]] = []
    for raw in versions:
        try:
            parsed.append((Version(raw), raw))
        except InvalidVersion:
            continue
    if not parsed:
        return sorted(versions)[-1] if versions else "0"
    stable = [(v, raw) for v, raw in parsed if not v.is_prerelease]
    pool = stable or parsed
    return max(pool, key=lambda item: item[0])[1]


class PyPISimpleConnector(BaseConnector):
    ecosystem = "pypi"
    protocol = "pypi-simple"

    def __init__(self, registry: RegistryInfo, timeout_seconds: float | None = None) -> None:
        settings = get_settings()
        self.registry = registry
        self.index_base_url = (registry.api_url or registry.base_url).rstrip("/")
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

    async def _get_with_retry(self, url: str, *, not_found_name: str | None = None):
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self.client.get(url, accept=SIMPLE_JSON_ACCEPT)
                if response.status_code == 404:
                    if not_found_name is not None:
                        raise PackageNotFound(not_found_name)
                    return None
                response.raise_for_status()
                return response
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
                    "simple index retry: registry=%s url=%s attempt=%s error=%s",
                    self.registry.slug,
                    url,
                    attempt,
                    exc.__class__.__name__,
                )
                await asyncio.sleep(delay)

    # --- project page parsing ------------------------------------------------

    @staticmethod
    def _files_from_json(payload: dict) -> list[dict]:
        files = []
        for item in payload.get("files") or []:
            files.append(
                {
                    "filename": item.get("filename") or "",
                    "url": item.get("url") or "",
                    "sha256": (item.get("hashes") or {}).get("sha256"),
                    "requires_python": item.get("requires-python"),
                    "yanked": bool(item.get("yanked")),
                    "size": item.get("size"),
                    "upload_time": item.get("upload-time"),
                    "has_core_metadata": bool(item.get("core-metadata") or item.get("data-dist-info-metadata")),
                }
            )
        return files

    def _files_from_html(self, html: str, page_url: str) -> list[dict]:
        collector = _AnchorCollector()
        collector.feed(html)
        files = []
        for anchor in collector.anchors:
            href = anchor["attrs"].get("href") or ""
            filename = anchor["text"].strip() or unquote(urlparse(href).path.rsplit("/", 1)[-1])
            if not filename:
                continue
            fragment = urlparse(href).fragment
            sha256 = fragment.split("=", 1)[1] if fragment.startswith("sha256=") else None
            files.append(
                {
                    "filename": filename,
                    "url": urljoin(page_url, href.split("#", 1)[0]),
                    "sha256": sha256,
                    "requires_python": anchor["attrs"].get("data-requires-python"),
                    "yanked": "data-yanked" in anchor["attrs"],
                    "size": None,
                    "upload_time": None,
                    "has_core_metadata": bool(
                        anchor["attrs"].get("data-core-metadata") or anchor["attrs"].get("data-dist-info-metadata")
                    ),
                }
            )
        return files

    async def _fetch_core_metadata_deps(self, files: list[dict]) -> tuple[list[NormalizedDependency], str | None]:
        """PEP 658: <file-url>.metadata for a wheel that advertises core metadata."""
        wheels = [f for f in files if f["filename"].endswith(".whl") and f["has_core_metadata"]]
        if not wheels:
            return [], None
        wheels.sort(key=lambda f: ("none-any" not in f["filename"], f["filename"]))
        response = await self._get_with_retry(wheels[0]["url"] + ".metadata")
        if response is None:
            return [], None
        message = Parser().parsestr(response.text, headersonly=True)
        dependencies: list[NormalizedDependency] = []
        for raw in message.get_all("Requires-Dist") or []:
            try:
                dep = parse_pep508(raw)
            except ValueError:
                continue
            dependencies.append(
                NormalizedDependency(
                    dependency_name=dep.dependency_name,
                    version_constraint=dep.version_constraint,
                    dependency_type=dep.dependency_type,
                    extra_name=dep.extra_name,
                    environment_markers=dep.environment_markers,
                    raw_spec=dep.raw_spec,
                )
            )
        return dependencies, message.get("Requires-Python")

    async def fetch_package_metadata(self, name: str) -> NormalizedPackage:
        normalized = normalize_package_name(name)
        page_url = f"{self.index_base_url}/{normalized}/"
        response = await self._get_with_retry(page_url, not_found_name=name)

        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            files = self._files_from_json(response.json())
        else:
            files = self._files_from_html(response.text, page_url)

        by_version: dict[str, list[dict]] = {}
        for item in files:
            version = _parse_filename_version(item["filename"])
            if version is None:
                continue
            by_version.setdefault(version, []).append(item)
        if not by_version:
            raise PackageNotFound(name)

        latest_version = _pick_latest(list(by_version))
        latest_deps, latest_requires_python = await self._fetch_core_metadata_deps(by_version[latest_version])

        versions: list[NormalizedVersion] = []
        for version, version_files in by_version.items():
            release_date = None
            for item in version_files:
                if item["upload_time"]:
                    parsed = _parse_datetime(item["upload_time"])
                    if parsed and (release_date is None or parsed > release_date):
                        release_date = parsed
            requires_python = next((f["requires_python"] for f in version_files if f["requires_python"]), None)
            if version == latest_version and latest_requires_python:
                requires_python = requires_python or latest_requires_python
            is_latest = version == latest_version
            versions.append(
                NormalizedVersion(
                    version=version,
                    release_date=release_date,
                    status="yanked" if all(f["yanked"] for f in version_files) else "active",
                    requires_python=requires_python,
                    raw_metadata={"source": "simple", "files": [f["filename"] for f in version_files]},
                    dependencies_hydrated=bool(is_latest and latest_deps),
                    artifacts=[
                        NormalizedArtifact(
                            filename=f["filename"],
                            download_url=f["url"],
                            size_bytes=f["size"],
                            checksum_sha256=f["sha256"],
                            checksum_md5=None,
                            artifact_type="wheel" if f["filename"].endswith(".whl") else "sdist",
                            platform_tags=None,
                            requires_dist_metadata=f["has_core_metadata"],
                        )
                        for f in version_files
                    ],
                    dependencies=latest_deps if is_latest else [],
                )
            )

        versions.sort(key=lambda v: v.release_date or datetime.min.replace(tzinfo=UTC), reverse=True)
        now = datetime.now(UTC)
        return NormalizedPackage(
            ecosystem="pypi",
            name=name,
            normalized_name=normalized,
            description=None,
            homepage_url=None,
            repository_url=None,
            documentation_url=None,
            license_raw=None,
            license_spdx=None,
            keywords=None,
            latest_version=latest_version,
            downloads_total=None,
            downloads_recent=None,
            created_at=now,
            updated_at=now,
            raw_metadata={"source": "simple", "index": self.index_base_url},
            versions=versions,
            vulnerabilities=[],
            authors=[],
            completeness="minimal",
        )

    async def fetch_version_dependencies(self, name: str, version: str) -> tuple[list[NormalizedDependency], str | None]:
        """Lazy hydration for a historical version: PEP 658 metadata of that
        version's wheel when the index publishes it, otherwise no data."""
        normalized = normalize_package_name(name)
        page_url = f"{self.index_base_url}/{normalized}/"
        response = await self._get_with_retry(page_url, not_found_name=name)
        content_type = response.headers.get("content-type", "")
        files = self._files_from_json(response.json()) if "json" in content_type else self._files_from_html(response.text, page_url)
        version_files = [f for f in files if _parse_filename_version(f["filename"]) == version]
        if not version_files:
            return [], None
        return await self._fetch_core_metadata_deps(version_files)

    # --- enumeration ----------------------------------------------------------

    async def list_package_names(self) -> list[str]:
        response = await self._get_with_retry(f"{self.index_base_url}/")
        if response is None:
            return []
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            projects = response.json().get("projects") or []
            names = [p.get("name", "") for p in projects]
        else:
            collector = _AnchorCollector()
            collector.feed(response.text)
            names = [a["text"].strip() for a in collector.anchors]
        return sorted({str(canonicalize_name(n)) for n in names if n})

    async def test_connection(self) -> tuple[bool, str]:
        try:
            response = await self.client.get(f"{self.index_base_url}/", accept=SIMPLE_JSON_ACCEPT)
        except RegistryAuthError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"{exc.__class__.__name__}: {exc}"
        if response.status_code >= 400:
            return False, f"index root status {response.status_code}"
        return True, f"status {response.status_code}, content-type {response.headers.get('content-type', '?')}"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
