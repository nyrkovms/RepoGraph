"""`pypi-simple` source adapter: indexes that speak only the Simple repository API (PEP 503 HTML, PEP 691 JSON) -
download.pytorch.org, pypi.nvidia.com, devpi (SPEC-MULTI-ECOSYSTEM.md §6.1, §7.1, §8.1).

An index knows files and nothing else: no description, license or serial. Dependencies come from the PEP 658
`.metadata` of one file of the version; versions whose files have none are reported as unavailable (Q14).
"""

import asyncio
import hashlib
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from packaging.tags import Tag
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)

from app.config import get_settings
from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
    FetchedDocument,
    NormalizedArtifact,
    NormalizedPackage,
    NormalizedVersion,
    PackageRef,
    RegistryConfig,
    SourceLookup,
    TrackedPackage,
    VersionDependencies,
    VersionStatus,
)
from app.ecosystems.pypi.adapters.warehouse import dependencies_from_requires_dist
from app.services.http import make_client
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

ACCEPT = "application/vnd.pypi.simple.v1+json, application/vnd.pypi.simple.v1+html;q=0.2, text/html;q=0.01"


class MetadataIntegrityError(Exception):
    """The `.metadata` we downloaded does not match the hash the index published for it: a source error."""


# --- the page of a project --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexFile:
    filename: str
    url: str                                   # absolute, without the fragment
    hashes: Mapping[str, str]
    requires_python: str | None
    yanked: bool
    yanked_reason: str | None
    has_metadata: bool
    metadata_hashes: Mapping[str, str]
    upload_time: datetime | None
    size: int | None


@dataclass(frozen=True, slots=True)
class ParsedFile:
    file: IndexFile
    version: str                               # as written in the file name
    kind: Literal["wheel", "sdist"]
    tags: frozenset[Tag]


def _split_hash(spec: str) -> tuple[str, str] | None:
    algorithm, sep, digest = spec.partition("=")
    return (algorithm.strip().lower(), digest.strip().lower()) if sep and algorithm and digest else None


def _metadata_flag(value: object) -> tuple[bool, Mapping[str, str]]:
    """PEP 658/714: `true` or `<algo>=<hex>` in HTML; a bool or a hash mapping in JSON."""
    if isinstance(value, Mapping):
        return True, {str(k).lower(): str(v).lower() for k, v in value.items()}
    if isinstance(value, bool):
        return value, {}
    if value is None:
        return False, {}
    text = str(value).strip().lower()
    if text in ("", "false"):
        return False, {}
    pair = _split_hash(text)
    return True, ({pair[0]: pair[1]} if pair else {})


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _filename_from_url(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _absolute(page_url: str, href: str) -> tuple[str, str]:
    """(URL without the fragment, fragment)."""
    parts = urlsplit(urljoin(page_url, href))
    return parts._replace(fragment="").geturl(), parts.fragment


class _AnchorCollector(HTMLParser):
    """PEP 503: every `<a>` is a file. The parser already unescapes attribute values (`&gt;=3.8`)."""

    def __init__(self, page_url: str) -> None:
        super().__init__()
        self.page_url = page_url
        self.files: list[IndexFile] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if not href:
            return
        url, fragment = _absolute(self.page_url, href)
        filename = _filename_from_url(url)
        if not filename:
            return
        pair = _split_hash(fragment)
        # PEP 714 renamed the attribute; either spelling, and a bare attribute means "true".
        metadata: str | None = None
        for name in ("data-core-metadata", "data-dist-info-metadata"):
            if name in attributes:
                metadata = attributes[name] or "true"
                break
        has_metadata, metadata_hashes = _metadata_flag(metadata)
        yanked = "data-yanked" in attributes
        self.files.append(IndexFile(
            filename=filename, url=url, hashes={pair[0]: pair[1]} if pair else {},
            requires_python=attributes.get("data-requires-python") or None,
            yanked=yanked, yanked_reason=(attributes.get("data-yanked") or None) if yanked else None,
            has_metadata=has_metadata, metadata_hashes=metadata_hashes,
            upload_time=_parse_time(attributes.get("data-upload-time")), size=None,
        ))


def parse_html_page(text: str, page_url: str) -> list[IndexFile]:
    collector = _AnchorCollector(page_url)
    collector.feed(text)
    collector.close()
    return collector.files


def parse_json_page(payload: Mapping[str, Any], page_url: str) -> list[IndexFile]:
    files: list[IndexFile] = []
    for item in payload.get("files") or []:
        if not isinstance(item, Mapping) or not item.get("url"):
            continue
        url, _ = _absolute(page_url, str(item["url"]))
        filename = item.get("filename") or _filename_from_url(url)
        if not filename:
            continue
        has_metadata, metadata_hashes = _metadata_flag(
            item["core-metadata"] if "core-metadata" in item else item.get("dist-info-metadata")
        )
        yanked, size = item.get("yanked"), item.get("size")
        files.append(IndexFile(
            filename=str(filename), url=url,
            hashes={str(k).lower(): str(v).lower() for k, v in (item.get("hashes") or {}).items()},
            requires_python=item.get("requires-python") or None,
            yanked=bool(yanked), yanked_reason=yanked if isinstance(yanked, str) and yanked.strip() else None,
            has_metadata=has_metadata, metadata_hashes=metadata_hashes,
            upload_time=_parse_time(item.get("upload-time")),
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        ))
    return files


def parse_page(response: httpx.Response) -> list[IndexFile]:
    page_url = str(response.url)
    if "json" in response.headers.get("content-type", "").lower():
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{page_url}: the PEP 691 answer is not an object")
        return parse_json_page(payload, page_url)
    return parse_html_page(response.text, page_url)


def parse_file_name(file: IndexFile) -> tuple[str, ParsedFile] | None:
    """(canonical project name, parsed file), or None for what is not a wheel or an sdist (eggs, .exe, ...)."""
    name = file.filename
    try:
        if name.endswith(".whl"):
            project, _, _, tags = parse_wheel_filename(name)
            # The version as written in the file name: `2.9.1` from an index and from pypi.org stay one version.
            return project, ParsedFile(file, name[:-4].split("-")[1], "wheel", tags)
        project, _ = parse_sdist_filename(name)
        stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else name[: -len(".zip")]
        return project, ParsedFile(file, stem.rpartition("-")[2], "sdist", frozenset())
    except (InvalidWheelFilename, InvalidSdistFilename):
        return None


def group_versions(ref: PackageRef, files: Sequence[IndexFile]) -> dict[str, list[ParsedFile]]:
    versions: dict[str, list[ParsedFile]] = {}
    for file in files:
        parsed = parse_file_name(file)
        if parsed is None or parsed[0] != ref.name:
            continue
        versions.setdefault(parsed[1].version, []).append(parsed[1])
    return versions


def _artifact(parsed: ParsedFile) -> NormalizedArtifact:
    file = parsed.file
    platform_tags: dict[str, object] | None = None
    if parsed.kind == "wheel":
        platform_tags = {
            "python": sorted({tag.interpreter for tag in parsed.tags}),
            "abi": sorted({tag.abi for tag in parsed.tags}),
            "platform": sorted({tag.platform for tag in parsed.tags}),
        }
    return NormalizedArtifact(
        filename=file.filename, artifact_type=parsed.kind, download_url=file.url, checksums=dict(file.hashes),
        size_bytes=file.size, platform_tags=platform_tags, has_metadata_file=file.has_metadata,
    )


def _version(version: str, files: Sequence[ParsedFile]) -> NormalizedVersion:
    yanked = all(parsed.file.yanked for parsed in files)
    reasons = [parsed.file.yanked_reason for parsed in files if parsed.file.yanked_reason]
    uploaded = [parsed.file.upload_time for parsed in files if parsed.file.upload_time]
    pythons = Counter(parsed.file.requires_python for parsed in files if parsed.file.requires_python)
    status: VersionStatus = "yanked" if yanked else "active"
    return NormalizedVersion(
        version=version, status=status, status_reason=reasons[0] if yanked and reasons else None,
        release_date=min(uploaded) if uploaded else None,
        engines={"python": pythons.most_common(1)[0][0]} if pythons else None,
        artifacts=[_artifact(parsed) for parsed in files],
        dependencies=None,
    )


# --- the metadata of a version ----------------------------------------------------------------------


def _cpython_key(parsed: ParsedFile) -> tuple[int, int]:
    best = (0, 0)
    for tag in parsed.tags:
        digits = tag.interpreter[2:] if tag.interpreter.startswith("cp") else ""
        if digits.isdigit() and len(digits) > 1:
            best = max(best, (int(digits[0]), int(digits[1:])))
    return best


def pick_metadata_file(files: Sequence[ParsedFile]) -> ParsedFile | None:
    """A universal wheel, else a manylinux x86_64 one with the newest CPython, else the first by file name.
    Wheels of one version for different platforms can carry different `Requires-Dist` (torch: Linux vs Windows)."""
    candidates = sorted((p for p in files if p.file.has_metadata), key=lambda p: p.file.filename)
    universal = [p for p in candidates if p.kind == "wheel" and any(tag.platform == "any" for tag in p.tags)]
    if universal:
        return universal[0]
    linux = [
        p for p in candidates
        if p.kind == "wheel" and any(tag.platform.startswith("manylinux") and tag.platform.endswith("_x86_64") for tag in p.tags)
    ]
    if linux:
        return max(linux, key=_cpython_key)   # the first of equals, i.e. the first by file name
    return candidates[0] if candidates else None


def parse_core_metadata(content: bytes) -> VersionDependencies:
    message = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
    requires_dist = [str(value) for value in message.get_all("Requires-Dist", [])]
    requires_python = str(message.get("Requires-Python") or "").strip()
    return VersionDependencies(
        dependencies=dependencies_from_requires_dist(requires_dist),
        engines={"python": requires_python} if requires_python else None,
    )


def _check_hash(url: str, content: bytes, expected: Mapping[str, str]) -> None:
    for algorithm, digest in expected.items():
        try:
            actual = hashlib.new(algorithm, content).hexdigest()
        except ValueError:
            continue   # an algorithm we do not know cannot be verified; another listed one may be
        if actual != digest:
            raise MetadataIntegrityError(f"{url}: {algorithm} is {actual}, the index published {digest}")
        return


# --- the adapter ------------------------------------------------------------------------------------


def _is_missing(response: httpx.Response) -> bool:
    return response.status_code == 404


class PyPISimpleAdapter:
    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry
        simple_url = registry.config.get("simple_url")
        self.root = (simple_url if isinstance(simple_url, str) and simple_url else registry.base_url).rstrip("/") + "/"
        settings = get_settings()
        self.timeout_seconds = settings.sync_http_timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds

    def page_url(self, ref: PackageRef) -> str:
        return f"{self.root}{quote(ref.name, safe='')}/"

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        response = await self.get(self.page_url(ref), {"Accept": ACCEPT})
        if _is_missing(response):
            return None
        versions = group_versions(ref, parse_page(response))
        return NormalizedPackage(
            ref=ref, registry_id=self.registry.id, display_name=ref.name, revision=response.headers.get("etag"),
            versions=[_version(version, files) for version, files in versions.items()],
        )

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies:
        response = await self.get(self.page_url(ref), {"Accept": ACCEPT})
        if _is_missing(response):
            return VersionDependencies([], available=False)
        chosen = pick_metadata_file(group_versions(ref, parse_page(response)).get(version, []))
        if chosen is None:
            return VersionDependencies([], available=False)

        url = chosen.file.url + ".metadata"
        document = await self.get(url, {})
        document.raise_for_status()   # the page promised this file; a 404 here is a source error
        _check_hash(url, document.content, chosen.file.metadata_hashes)
        result = parse_core_metadata(document.content)
        base = self.registry.base_url.rstrip("/") + "/"
        result.documents = [FetchedDocument(
            path=url[len(base):] if url.startswith(base) else url, content=document.content,
            content_type="text/plain", etag=document.headers.get("etag"),
        )]
        return result

    async def exists(self, ref: PackageRef) -> bool:
        return not _is_missing(await self.get(self.page_url(ref), {"Accept": ACCEPT}))

    async def test_connection(self) -> None:
        # Like the warehouse adapter, a root that is not there fails: a wrong base_url is the usual mistake,
        # and an index that serves project pages but no root is not one we know how to read.
        (await self.get(self.root, {"Accept": ACCEPT})).raise_for_status()

    def change_detector(self) -> ChangeDetector:
        return SimpleChangeDetector(self)

    def min_interval_seconds(self) -> float:
        return 0.0

    async def get(self, url: str, headers: Mapping[str, str]) -> httpx.Response:
        """GET with retries on transient errors. 404 and 304 are answers; any other error status is raised."""
        attempt = 0
        while True:
            attempt += 1
            try:
                async with make_client(timeout=self.timeout_seconds) as client:
                    response = await client.get(url, headers=dict(headers), follow_redirects=True)
                if response.status_code not in (304, 404):
                    response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001
                if not is_retryable_upstream_error(exc) or attempt > self.max_retries:
                    raise
                delay = compute_backoff_delay_seconds(
                    attempt=attempt, base_seconds=self.retry_base_seconds, max_seconds=self.retry_max_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                logger.warning("Simple index retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay, exc.__class__.__name__)
                await asyncio.sleep(delay)


class SimpleChangeDetector:
    """A conditional GET of every tracked project page; the ETag is the revision."""

    def __init__(self, adapter: PyPISimpleAdapter, concurrency: int | None = None) -> None:
        self.adapter = adapter
        self.concurrency = concurrency or get_settings().daily_update_concurrency

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(package: TrackedPackage) -> tuple[TrackedPackage, Literal["changed", "missing", "same"], str | None]:
            known = package.revision or package.etag
            if known is None:
                return package, "changed", None
            async with semaphore:
                response = await self.adapter.get(
                    self.adapter.page_url(package.ref), {"Accept": ACCEPT, "If-None-Match": known}
                )
            if _is_missing(response):
                # Already known to be unavailable: nothing new to report until it comes back.
                return package, "same" if package.unavailable else "missing", None
            if response.status_code == 304:
                return package, "changed" if package.unavailable else "same", known
            return package, "changed", response.headers.get("etag")

        changed: list[tuple[TrackedPackage, str | None]] = []
        missing: list[TrackedPackage] = []
        for package, outcome, revision in await asyncio.gather(*(one(package) for package in tracked)):
            if outcome == "changed":
                changed.append((package, revision))
            elif outcome == "missing":
                missing.append(package)
        # Nothing global to remember: every page answers for itself.
        return ChangeSet(changed=changed, missing=missing, cursor={})
