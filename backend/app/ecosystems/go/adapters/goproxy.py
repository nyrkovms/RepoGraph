"""`goproxy` source adapter: proxy.golang.org and any other server of the GOPROXY protocol (Athens, corporate
proxies) (SPEC-MULTI-ECOSYSTEM.md §6.1, §7.1, §7.2).

A module is `<base>/<escaped path>/@v/list` (its tagged versions, one per line, unordered, no pseudo-versions), a
version is `<version>.info` (its time), `.mod` (its go.mod) and `.zip`. A module with no tag has an empty list and a
pseudo-version as `@latest`, which is then its only version. The proxy has no ETag for the list and publishes no
checksums, description, license or authors: the revision of a package is a hash of its version list, and the rest stays
empty (§7.1, Q11).

Dependencies are read from the go.mod of the latest version at once and lazily for the others
(`fetch_version_dependencies`). The same go.mod gives `retract` (the covered versions are `yanked`) and
`// Deprecated:`. `replace` and `exclude` of a dependency are not applied (§9.5).
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Literal

import httpx

from app.config import get_settings
from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
    FetchedDocument,
    InvalidVersion,
    NormalizedArtifact,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    PackageRef,
    RegistryConfig,
    SourceLookup,
    TrackedPackage,
    VersionDependencies,
)
# A module without tags has an empty list; its revision is the pseudo-version `@latest` names (`latest:<version>`),
# so a new commit asked for through the proxy shows up; the index detector counts pseudo-versions for it alone.
from app.ecosystems.go.changes import TAGLESS, GoIndexChangesDetector
from app.ecosystems.go.coordinates import GoCoordinates, escape_path, escape_version
from app.ecosystems.go.gomod import GoMod, GoModError, Retract, parse_go_mod
from app.ecosystems.go.versions import GoVersionScheme, compare, is_incompatible
from app.services.http import make_client
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

_COORDINATES = GoCoordinates()
_SCHEME = GoVersionScheme()
# Go proxies answer "no such module or version" with 404, and 410 for what they know was removed.
_MISSING = (404, 410)
_TEST_MODULE = "golang.org/x/mod"


# --- pure helpers ---------------------------------------------------------------------------------------


def list_revision(versions: Collection[str]) -> str:
    """The revision of a package: the list has no ETag, so a hash of the sorted versions stands for it."""
    return hashlib.sha256("\n".join(sorted(versions)).encode()).hexdigest()



def _is_missing(response: httpx.Response) -> bool:
    return response.status_code in _MISSING


def _list_lines(response: httpx.Response) -> list[str]:
    return [line.strip() for line in response.text.splitlines() if line.strip()]


def _date(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _info_time(response: httpx.Response) -> datetime | None:
    try:
        document = json.loads(response.text)
    except ValueError:
        return None
    return _date(document.get("Time")) if isinstance(document, dict) else None


def _engines(go_mod: GoMod) -> dict[str, str] | None:
    engines = {name: value for name, value in (("go", go_mod.go), ("toolchain", go_mod.toolchain)) if value}
    return engines or None


def dependencies_of(go_mod: GoMod) -> list[NormalizedDependency]:
    result: list[NormalizedDependency] = []
    for require in go_mod.requires:
        try:
            target = _COORDINATES.parse(require.path)
        except ValueError:
            logger.warning("Skipping Go dependency with an invalid module path %r in %s", require.path, go_mod.module)
            continue
        result.append(NormalizedDependency(
            target=target, constraint=require.version, dependency_type="runtime", scope="require",
            raw_spec=f"{require.path} {require.version}" + (" // indirect" if require.indirect else ""),
            details={"indirect": True} if require.indirect else None,
        ))
    return result


def _covers(retract: Retract, version: str) -> bool:
    try:
        return compare(retract.low, version) <= 0 and compare(version, retract.high) <= 0
    except InvalidVersion:
        return False


def retraction(go_mod: GoMod, version: str) -> str | None:
    """Why `version` is retracted by this go.mod ("retracted" when it gives no rationale), None if it is not."""
    for retract in go_mod.retracts:
        if _covers(retract, version):
            return retract.rationale or "retracted"
    return None


def _pick_latest(versions: Sequence[str]) -> str | None:
    """The highest release, else the highest prerelease (pseudo-versions are prereleases)."""
    releases = [v for v in versions if not _SCHEME.is_prerelease(v)]
    pool = releases or list(versions)
    return max(pool, key=_SCHEME.sort_key, default=None)


# --- the adapter ----------------------------------------------------------------------------------------


class GoProxyAdapter:
    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry
        self.root = registry.base_url.rstrip("/") + "/"
        settings = get_settings()
        self.concurrency = settings.daily_update_concurrency
        self.timeout_seconds = settings.sync_http_timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds

    # --- urls ---

    def module_url(self, ref: PackageRef) -> str:
        return f"{self.root}{escape_path(ref.name)}/"

    def list_url(self, ref: PackageRef) -> str:
        return f"{self.module_url(ref)}@v/list"

    def latest_url(self, ref: PackageRef) -> str:
        return f"{self.module_url(ref)}@latest"

    def version_url(self, ref: PackageRef, version: str, suffix: str) -> str:
        return f"{self.module_url(ref)}@v/{escape_version(version)}.{suffix}"

    # --- SourceAdapter ---

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        response = await self.get(self.list_url(ref))
        listed = [] if _is_missing(response) else _list_lines(response)
        known: dict[str, datetime | None] = {}
        # `@latest` is the proxy's answer to `go get path@latest`: it skips +incompatible tags above a module's real
        # latest (go-sqlite3 v2.0.3+incompatible) and takes them when nothing newer has a go.mod (docker v28).
        document: Mapping[str, object] | None = None
        try:
            latest = await self.get(self.latest_url(ref))
            document = None if _is_missing(latest) else _json_object(latest)
        except (httpx.HTTPError, ValueError):
            if not listed:
                raise
            # The list is enough to know the module; the latest falls back to the scheme's highest.
            logger.warning("@latest of %s in %s failed", ref.name, self.registry.slug, exc_info=True)
        named = document.get("Version") if document is not None else None
        if listed:
            raw_versions = listed
        else:
            # No tags (or not a module): the only version is whatever `@latest` says.
            if not isinstance(named, str):
                if document is None:
                    return None
                raise ValueError(f"{self.latest_url(ref)}: the answer has no Version")
            raw_versions, known = [named], {named: _date(document.get("Time") if document else None)}

        versions = self._valid_versions(ref, raw_versions)
        dates = await self._release_dates(ref, versions, known)
        top = named if isinstance(named, str) and named in versions else _pick_latest(versions)
        go_mod = await self._latest_go_mod(ref, top) if top is not None else None
        # Go takes retractions from the highest version, retracted or not (a newest tag may retract itself, and then
        # `@latest` names an older one whose go.mod knows nothing of it): read both.
        highest = _pick_latest([v for v in versions if not is_incompatible(v)]) or top
        retracting = [go_mod] if go_mod is not None else []
        if highest is not None and highest != top:
            extra = await self._latest_go_mod(ref, highest)
            retracting += [extra] if extra is not None else []

        normalized: list[NormalizedVersion] = []
        for version in versions:
            reason = next((r for mod in retracting if (r := retraction(mod, version)) is not None), None)
            is_top = version == top and go_mod is not None
            normalized.append(NormalizedVersion(
                version=version, status="yanked" if reason else "active", status_reason=reason,
                release_date=dates.get(version), engines=_engines(go_mod) if is_top and go_mod else None,
                raw_metadata=_raw_metadata(go_mod) if is_top and go_mod else {},
                artifacts=[self._artifact(ref, version)],
                dependencies=dependencies_of(go_mod) if is_top and go_mod else None,
            ))
        return NormalizedPackage(
            ref=ref, registry_id=self.registry.id, display_name=_COORDINATES.display(ref),
            revision=list_revision(listed) if listed else f"{TAGLESS}{raw_versions[0]}", versions=normalized,
            dist_tags={"latest": top} if top is not None and top == named else None,
            deprecation={"message": go_mod.deprecated} if go_mod is not None and go_mod.deprecated else None,
        )

    async def fetch_version_dependencies(
        self, ref: PackageRef, version: str, lookup: SourceLookup,
    ) -> VersionDependencies:
        response = await self.get(self.version_url(ref, version, "mod"))
        if _is_missing(response):
            return VersionDependencies([], available=False)
        try:
            go_mod = parse_go_mod(response.text)
        except GoModError as exc:
            logger.warning("Unreadable go.mod of %s@%s in %s: %s", ref.name, version, self.registry.slug, exc)
            return VersionDependencies([], available=False)
        document = FetchedDocument(
            path=f"{escape_path(ref.name)}/@v/{escape_version(version)}.mod", content=response.content,
            content_type="text/plain", etag=response.headers.get("etag"),
        )
        return VersionDependencies(dependencies_of(go_mod), engines=_engines(go_mod), documents=[document])

    async def fetch_document(self, path: str) -> FetchedDocument | None:
        """A document by its path relative to the base, already escaped (the go.mod of a module outside the catalog)."""
        response = await self.get(self.root + path.lstrip("/"))
        if _is_missing(response):
            return None
        return FetchedDocument(
            path=path, content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            etag=response.headers.get("etag"),
        )

    async def exists(self, ref: PackageRef) -> bool:
        response = await self.get(self.list_url(ref))
        if not _is_missing(response) and _list_lines(response):
            return True
        return not _is_missing(await self.get(self.latest_url(ref)))

    async def test_connection(self) -> None:
        # A proxy that does not hold this module is still a proxy: only a failure to answer is an error.
        await self.get(f"{self.root}{_TEST_MODULE}/@latest")

    def change_detector(self) -> ChangeDetector:
        index_url = self.registry.config.get("index_url")
        if isinstance(index_url, str) and index_url:
            # proxy.golang.org: the module index; full checks go module by module through the list.
            return GoIndexChangesDetector(self.registry, index_url, reconciler=GoListChangeDetector(self))
        return GoListChangeDetector(self)

    def min_interval_seconds(self) -> float:
        return 0.0

    # --- pieces ---

    def _valid_versions(self, ref: PackageRef, raw: Sequence[str]) -> list[str]:
        versions: list[str] = []
        for version in dict.fromkeys(raw):
            try:
                _SCHEME.validate(version)
            except InvalidVersion:
                logger.warning("Skipping Go module %s version %r: not a version Go accepts", ref.name, version)
                continue
            versions.append(version)
        return versions

    async def _release_dates(
        self, ref: PackageRef, versions: Sequence[str], known: Mapping[str, datetime | None],
    ) -> dict[str, datetime | None]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(version: str) -> tuple[str, datetime | None]:
            if version in known:
                return version, known[version]
            try:
                async with semaphore:
                    response = await self.get(self.version_url(ref, version, "info"))
                return version, None if _is_missing(response) else _info_time(response)
            except Exception:  # noqa: BLE001 - a missing date must not lose the version
                logger.warning("No release date for %s@%s in %s", ref.name, version, self.registry.slug, exc_info=True)
                return version, None

        return dict(await asyncio.gather(*(one(version) for version in versions)))

    async def _latest_go_mod(self, ref: PackageRef, version: str) -> GoMod | None:
        try:
            response = await self.get(self.version_url(ref, version, "mod"))
            if _is_missing(response):
                logger.warning("%s@%s has no go.mod in %s", ref.name, version, self.registry.slug)
                return None
            return parse_go_mod(response.text)
        except GoModError as exc:
            logger.warning("Unreadable go.mod of %s@%s in %s: %s", ref.name, version, self.registry.slug, exc)
        except httpx.HTTPError:
            logger.warning("go.mod of %s@%s in %s failed", ref.name, version, self.registry.slug, exc_info=True)
        return None

    def _artifact(self, ref: PackageRef, version: str) -> NormalizedArtifact:
        return NormalizedArtifact(
            filename=f"{ref.name.rpartition('/')[2]}@{version}.zip", artifact_type="zip",
            download_url=self.version_url(ref, version, "zip"),
        )

    async def get(self, url: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
        """GET with retries on transient errors. 404, 410 and 304 are answers; any other error status is raised."""
        attempt = 0
        while True:
            attempt += 1
            try:
                async with make_client(timeout=self.timeout_seconds) as client:
                    response = await client.get(url, headers=dict(headers or {}), follow_redirects=True)
                if response.status_code not in (304, *_MISSING):
                    response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001
                if not is_retryable_upstream_error(exc) or attempt > self.max_retries:
                    raise
                delay = compute_backoff_delay_seconds(
                    attempt=attempt, base_seconds=self.retry_base_seconds, max_seconds=self.retry_max_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                logger.warning("goproxy retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay, exc.__class__.__name__)
                await asyncio.sleep(delay)


def _json_object(response: httpx.Response) -> Mapping[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{response.url}: the proxy answer is not an object")
    return payload


def _raw_metadata(go_mod: GoMod) -> dict[str, object]:
    fields = {name: value for name, value in (("go", go_mod.go), ("toolchain", go_mod.toolchain)) if value}
    return {"go_mod": fields} if fields else {}


class GoListChangeDetector:
    """A full GET of `@v/list` for every tracked module (it has no ETag); the hash of the list is the revision."""

    def __init__(self, adapter: GoProxyAdapter, concurrency: int | None = None) -> None:
        self.adapter = adapter
        self.concurrency = concurrency or get_settings().daily_update_concurrency

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(package: TrackedPackage) -> tuple[TrackedPackage, Literal["changed", "missing", "same"], str | None]:
            if package.revision is None:
                return package, "changed", None
            try:
                async with semaphore:
                    response = await self.adapter.get(self.adapter.list_url(package.ref))
                listed = [] if _is_missing(response) else _list_lines(response)
                latest = None
                if not listed:
                    async with semaphore:
                        latest = await self.adapter.get(self.adapter.latest_url(package.ref))
            except Exception:  # noqa: BLE001 - one module failing must not fail the whole source for the day
                logger.warning("Change check of %s in %s failed", package.ref.name, self.adapter.registry.slug,
                               exc_info=True)
                return package, "same", None
            if not listed and (latest is None or _is_missing(latest)):
                # Already known to be unavailable: nothing new to report until it comes back.
                return package, "same" if package.unavailable else "missing", None
            try:
                named = _json_object(latest).get("Version") if latest is not None else None
            except ValueError:
                logger.warning("@latest of %s in %s is not JSON", package.ref.name, self.adapter.registry.slug)
                return package, "same", None
            revision = list_revision(listed) if listed else f"{TAGLESS}{named}"
            if revision == package.revision:
                return package, "changed" if package.unavailable else "same", revision
            return package, "changed", revision

        changed: list[tuple[TrackedPackage, str | None]] = []
        missing: list[TrackedPackage] = []
        for package, outcome, revision in await asyncio.gather(*(one(package) for package in tracked)):
            if outcome == "changed":
                changed.append((package, revision))
            elif outcome == "missing":
                missing.append(package)
        return ChangeSet(changed=changed, missing=missing, cursor={})
