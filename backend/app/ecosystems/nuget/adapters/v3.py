"""`nuget-v3` source adapter: nuget.org and any other NuGet v3 feed (SPEC-MULTI-ECOSYSTEM.md §6.1, §7.1, §7.2).

`registry.base_url` is the feed's service index. Its registrations resource (the SemVer 2 one when the feed has it)
serves a package as `<id lowercase>/index.json`: pages of versions, each page inline or behind its own URL. Every
version's entry carries its dependency groups, deprecation and listing state, so, as with Cargo, the dependencies of
all versions are in the one document and nothing is fetched lazily. The ETag of that document is the package's
revision.

The entries also hold `vulnerabilities`; they are not stored (D6). Unlisted versions stay in the catalog with status
`unlisted`: a lock file may still pin them.
"""

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
    InvalidVersion,
    NormalizedArtifact,
    NormalizedAuthor,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    PackageRef,
    RegistryConfig,
    SourceLookup,
    TrackedPackage,
    VersionDependencies,
)
from app.ecosystems.nuget.changes import NuGetCatalogChangesDetector
from app.ecosystems.nuget.coordinates import NuGetCoordinates, registration_path
from app.ecosystems.nuget.versions import NuGetVersionScheme, normalize
from app.services.http import make_client
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

_COORDINATES = NuGetCoordinates()
_SCHEME = NuGetVersionScheme()

# Registrations resource types, best first: SemVer 2 (prerelease labels with dots, build metadata), then whatever the
# feed offers. nuget.org lists the same URL under several of them.
_REGISTRATION_TYPES = ("RegistrationsBaseUrl/3.6.0", "RegistrationsBaseUrl/Versioned", "RegistrationsBaseUrl")
_FLAT_CONTAINER_TYPE = "PackageBaseAddress/3.0.0"

# nuget.org dates the versions that were never listed 1900-01-01.
_NO_DATE_YEAR = 1900


@dataclass(frozen=True, slots=True)
class ServiceIndex:
    registrations: str       # with a trailing slash
    flat_container: str | None


# --- the service index ------------------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _date(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return None if stamp is not None and stamp.year <= _NO_DATE_YEAR else stamp


def _json_object(response: httpx.Response) -> Mapping[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{response.url}: the feed answer is not an object")
    return payload


def parse_service_index(document: Mapping[str, object], url: str) -> ServiceIndex:
    resources = document.get("resources")
    if not isinstance(resources, list):
        raise ValueError(f"{url}: no `resources`, not a NuGet v3 service index")
    found: dict[str, str] = {}
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        kind, location = resource.get("@type"), _text(resource.get("@id"))
        if isinstance(kind, str) and location is not None:
            found.setdefault(kind, location)
    registrations = next((found[kind] for kind in _REGISTRATION_TYPES if kind in found), None)
    if registrations is None:
        raise ValueError(f"{url}: the feed has no RegistrationsBaseUrl resource")
    flat = found.get(_FLAT_CONTAINER_TYPE)
    return ServiceIndex(registrations.rstrip("/") + "/", flat.rstrip("/") + "/" if flat else None)


# --- one version ------------------------------------------------------------------------------------


def _groups(entry: Mapping[str, object]) -> list[Mapping[str, object]]:
    groups = entry.get("dependencyGroups")
    return [group for group in (groups if isinstance(groups, list) else []) if isinstance(group, Mapping)]


def normalize_dependencies(entry: Mapping[str, object]) -> list[NormalizedDependency]:
    """The dependency groups of one `catalogEntry` in contract form, in document order (SPEC §7.2)."""
    groups = _groups(entry)
    # Every group counts, an empty one too: it is what a project on that framework takes instead of the others.
    frameworks = [_text(group.get("targetFramework")) or "" for group in groups]
    result: list[NormalizedDependency] = []
    for group, framework in zip(groups, frameworks, strict=True):
        listed = group.get("dependencies")
        for dep in (listed if isinstance(listed, list) else []):
            package_id = dep.get("id") if isinstance(dep, Mapping) else None
            if not isinstance(package_id, str):
                logger.warning("Skipping NuGet dependency without an id: %r", dep)
                continue
            try:
                target = _COORDINATES.parse(package_id)
            except ValueError:
                logger.warning("Skipping NuGet dependency with a malformed id %r", package_id)
                continue
            requirement = _text(dep.get("range")) or ""
            details: dict[str, object] = {"groups": frameworks}
            for key in ("exclude", "include"):
                if _text(dep.get(key)):
                    details[key] = dep[key]
            result.append(NormalizedDependency(
                target=target, constraint=requirement, dependency_type="runtime", scope="dependency",
                raw_spec=f"{package_id} {requirement}".rstrip(), environment_markers={"dotnet_target_framework": framework},
                details=details,
            ))
    return result


def _deprecation(entry: Mapping[str, object]) -> dict[str, object] | None:
    raw = entry.get("deprecation")
    if not isinstance(raw, Mapping):
        return None
    deprecation: dict[str, object] = {}
    reasons = raw.get("reasons")
    if isinstance(reasons, list):
        deprecation["reasons"] = [reason for reason in reasons if isinstance(reason, str)]
    if _text(raw.get("message")):
        deprecation["message"] = raw["message"]
    alternate = raw.get("alternatePackage")
    if isinstance(alternate, Mapping) and isinstance(alternate.get("id"), str):
        deprecation["alternate"] = {
            key: alternate[key] for key in ("id", "range") if isinstance(alternate.get(key), str)
        }
    return deprecation


def _download_url(entry: Mapping[str, object], package_id: str, version: str, flat_container: str | None) -> str | None:
    content = _text(entry.get("packageContent"))
    if content is not None or flat_container is None:
        return content
    return f"{flat_container}{package_id}/{version}/{package_id}.{version}.nupkg"


def version_from_entry(
    entry: Mapping[str, object], ref: PackageRef, flat_container: str | None = None,
) -> NormalizedVersion | None:
    """One `catalogEntry`; None if its version is not one the NuGet scheme accepts."""
    raw = entry.get("version")
    if not isinstance(raw, str):
        return None
    try:
        version = normalize(raw)
    except InvalidVersion:
        logger.warning("Skipping NuGet package %s version %r: not a version NuGet accepts", ref.name, raw)
        return None
    package_id = registration_path(ref)
    return NormalizedVersion(
        version=version,
        status="active" if entry.get("listed") is not False else "unlisted",
        release_date=_date(entry.get("published")),
        deprecation=_deprecation(entry),
        artifacts=[NormalizedArtifact(
            filename=f"{package_id}.{version}.nupkg", artifact_type="nupkg",
            download_url=_download_url(entry, package_id, version, flat_container),
        )],
        dependencies=normalize_dependencies(entry),
    )


# --- the package ------------------------------------------------------------------------------------


def _authors(entry: Mapping[str, object]) -> list[NormalizedAuthor] | None:
    raw = entry.get("authors")
    names = [name.strip() for name in raw.split(",")] if isinstance(raw, str) else []
    return [NormalizedAuthor(name=name, email=None, url=None, role="author") for name in names if name] or None


def _tags(value: object) -> list[str] | None:
    # Old packages carry `[""]`.
    tags = [tag.strip() for tag in value if isinstance(tag, str)] if isinstance(value, list) else []
    return [tag for tag in tags if tag] or None


def apply_entry(package: NormalizedPackage, entry: Mapping[str, object]) -> None:
    """Fill the package-level fields from the entry of its newest listed version."""
    expression, url = _text(entry.get("licenseExpression")), _text(entry.get("licenseUrl"))
    package.description = _text(entry.get("description")) or _text(entry.get("summary"))
    package.homepage_url = _text(entry.get("projectUrl"))
    package.license_raw = expression or url
    package.license_spdx = expression
    package.keywords = _tags(entry.get("tags"))
    package.authors = _authors(entry)
    deprecation = _deprecation(entry)
    if deprecation is not None:
        # A version deprecated with reasons only (Legacy, CriticalBugs) still says why.
        reasons = deprecation.get("reasons")
        package.deprecation = {"message": deprecation.get("message") or ", ".join(reasons if isinstance(reasons, list) else [])}


def _normalized(entry: Mapping[str, object]) -> str | None:
    raw = entry.get("version")
    try:
        return normalize(raw) if isinstance(raw, str) else None
    except InvalidVersion:
        return None


def _newest(entries: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The entry of the newest version, the newest listed one when there is any (versions the scheme rejects have
    no place in the order)."""
    valid = [entry for entry in entries if _normalized(entry) is not None]
    candidates = [entry for entry in valid if entry.get("listed") is not False] or valid
    return max(candidates, key=lambda entry: _SCHEME.sort_key(str(_normalized(entry))), default=None)


# --- the adapter ------------------------------------------------------------------------------------


def _is_missing(response: httpx.Response) -> bool:
    return response.status_code == 404


def _revision(response: httpx.Response) -> str:
    # A feed without ETags (a plain file server) still has a revision: the content.
    return response.headers.get("etag") or hashlib.sha256(response.content).hexdigest()


class NuGetV3Adapter:
    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry
        settings = get_settings()
        self.timeout_seconds = settings.sync_http_timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds
        self._service: ServiceIndex | None = None

    async def _read_service_index(self) -> ServiceIndex:
        response = await self.get(self.registry.base_url, {})
        response.raise_for_status()   # get() lets 404 through; for the service index it is an error
        return parse_service_index(_json_object(response), str(response.url))

    async def service_index(self) -> ServiceIndex:
        # The same for every package of the feed: read once per adapter.
        if self._service is None:
            self._service = await self._read_service_index()
        return self._service

    async def registration_url(self, ref: PackageRef) -> str:
        return f"{(await self.service_index()).registrations}{quote(registration_path(ref), safe='')}/index.json"

    async def _entries(self, document: Mapping[str, object]) -> list[Mapping[str, object]]:
        """The `catalogEntry` of every version of a registration index; pages without `items` are fetched."""
        listed_pages = document.get("items")
        pages = [page for page in (listed_pages if isinstance(listed_pages, list) else []) if isinstance(page, Mapping)]

        async def items(page: Mapping[str, object]) -> object:
            if page.get("items") is not None:
                return page["items"]
            response = await self.get(str(page["@id"]), {})
            response.raise_for_status()
            return _json_object(response).get("items")

        entries: list[Mapping[str, object]] = []
        for listed in await asyncio.gather(*(items(page) for page in pages)):
            for item in (listed if isinstance(listed, list) else []):
                entry = item.get("catalogEntry") if isinstance(item, Mapping) else None
                if isinstance(entry, Mapping):
                    # `packageContent` is on the item; nuget.org repeats it in the entry, other feeds may not.
                    entries.append({"packageContent": item.get("packageContent"), **entry})
        return entries

    async def _registration(self, ref: PackageRef) -> tuple[httpx.Response, list[Mapping[str, object]]] | None:
        response = await self.get(await self.registration_url(ref), {})
        if _is_missing(response):
            return None
        return response, await self._entries(_json_object(response))

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        found = await self._registration(ref)
        if found is None:
            return None
        response, entries = found
        flat = (await self.service_index()).flat_container
        versions = [version for entry in entries if (version := version_from_entry(entry, ref, flat)) is not None]
        package = NormalizedPackage(
            ref=ref, registry_id=self.registry.id, display_name=_COORDINATES.display(ref),
            revision=_revision(response), versions=versions,
        )
        newest = _newest(entries)
        if newest is not None:
            package.display_name = _text(newest.get("id")) or package.display_name
            apply_entry(package, newest)
        return package

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies:
        found = await self._registration(ref)
        if found is None:
            return VersionDependencies([], available=False)
        try:
            wanted = normalize(version)
        except InvalidVersion:
            return VersionDependencies([], available=False)
        for entry in found[1]:
            if _normalized(entry) == wanted:
                return VersionDependencies(dependencies=normalize_dependencies(entry))
        return VersionDependencies([], available=False)

    async def exists(self, ref: PackageRef) -> bool:
        # A full GET: a CDN in front of a static feed may not answer HEAD.
        response = await self.get(await self.registration_url(ref), {})
        return not _is_missing(response)

    async def test_connection(self) -> None:
        await self._read_service_index()

    def change_detector(self) -> ChangeDetector:
        etag = NuGetEtagChangeDetector(self)
        catalog_url = self.registry.config.get("catalog_url")
        if isinstance(catalog_url, str) and catalog_url.strip():
            return NuGetCatalogChangesDetector(self.registry, catalog_url.strip(), reconciler=etag)
        return etag

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
                logger.warning("NuGet feed retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay, exc.__class__.__name__)
                await asyncio.sleep(delay)


class NuGetEtagChangeDetector:
    """A conditional GET of every tracked registration index; the ETag is the revision."""

    def __init__(self, adapter: NuGetV3Adapter, concurrency: int | None = None) -> None:
        self.adapter = adapter
        self.concurrency = concurrency or get_settings().daily_update_concurrency

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        semaphore = asyncio.Semaphore(self.concurrency)
        if tracked:
            try:
                await self.adapter.service_index()   # read once here, not by every parallel check below
            except Exception:  # noqa: BLE001 - each package then fails (and is logged) on its own
                logger.warning("Service index of %s failed", self.adapter.registry.slug, exc_info=True)

        async def one(package: TrackedPackage) -> tuple[TrackedPackage, Literal["changed", "missing", "same"], str | None]:
            known = package.revision or package.etag
            if known is None:
                return package, "changed", None
            try:
                async with semaphore:
                    url = await self.adapter.registration_url(package.ref)
                    response = await self.adapter.get(url, {"If-None-Match": known})
            except Exception:  # noqa: BLE001 - one package failing must not fail the whole source for the day
                logger.warning("Change check of %s in %s failed", package.ref.name, self.adapter.registry.slug,
                               exc_info=True)
                return package, "same", None
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
        # Nothing global to remember: every registration index answers for itself.
        return ChangeSet(changed=changed, missing=missing, cursor={})
