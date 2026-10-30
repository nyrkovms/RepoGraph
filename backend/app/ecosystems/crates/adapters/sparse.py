"""`cargo-sparse` source adapter: index.crates.io and other registries that serve a Cargo sparse index
(SPEC-MULTI-ECOSYSTEM.md §6.1, §7.1, §7.2).

A crate is one file of the index, `<index_path>`: a JSON object per line, one per version, oldest first, each with the
dependencies, features, checksum, `yanked`, `rust_version` and `pubtime` (the release date) of that version. Unlike npm
nothing is fetched lazily: the dependencies of every version are in the same document. The file's ETag is the
package's revision.

Description, links, keywords, downloads and the license are not in the index. crates.io has them in its API, which
allows one request per second for everyone: `fetch_package` reads the index alone, so adding a crate (lazily, inside a
resolve) costs no API turn, and `fetch_api_fields` is called in the background for crates added or changed since
(tasks/crates_api.py, SPEC Q22). A registry without `config.api_url` has no such fields.

The index serves a crate under its lowercased name, so `Serde` would find serde: an answer for another spelling than
the published one is no answer, and adding a package maps the spelling through the API instead (Q21).
"""

import asyncio
import json
import logging
import re
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Literal
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
    DependencyType,
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
from app.ecosystems.crates.coordinates import CratesCoordinates, dependency_ref, index_path
from app.ecosystems.crates.versions import parse as parse_version
from app.services.http import make_client
from app.services.rate_limit import wait_turn
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

# crates.io asks for at most one API request per second from everyone.
API_INTERVAL_SECONDS = 1.0

_COORDINATES = CratesCoordinates()

# `kind` of a dependency -> our dependency type; a missing kind is `normal`.
_KIND_TYPES: dict[str, DependencyType] = {"normal": "runtime", "dev": "dev", "build": "build"}

# Cargo substitutes these in the `dl` of config.json; with none of them the URL is `<dl>/<crate>/<version>/download`.
_DL_MARKERS = ("{crate}", "{version}", "{prefix}", "{lowerprefix}", "{sha256-checksum}")


# --- features and optional dependencies -------------------------------------------------------------


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _date(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def merged_features(entry: Mapping[str, object]) -> dict[str, list[str]]:
    """`features` plus `features2`: the index keeps the features that use `dep:` and `x?/feat` (syntax old Cargo
    cannot read) apart, under `"v": 2`."""
    merged: dict[str, list[str]] = {}
    for key in ("features", "features2"):
        document = entry.get(key)
        for name, values in (document.items() if isinstance(document, Mapping) else ()):
            if isinstance(values, list):
                merged[name] = [value for value in values if isinstance(value, str)]
    return merged


def _dep_references(features: Mapping[str, Sequence[str]]) -> set[str]:
    return {value[4:] for values in features.values() for value in values if value.startswith("dep:")}


def implicit_features(features: Mapping[str, Sequence[str]], optional: Collection[str]) -> set[str]:
    """An optional dependency `x` is a feature of its own turning it on, unless some value says `dep:x`."""
    return set(optional) - _dep_references(features)


def enabled_by(features: Mapping[str, Sequence[str]], optional: Collection[str]) -> dict[str, list[str]]:
    """For each optional dependency (by local name), the sorted features whose activation turns it on, directly or
    through other features.

    A value `dep:x` turns on `x`. `x/feat` turns on `x` when `x` is an optional dependency (otherwise `x` is a feature
    or a non-optional dependency and nothing is switched on); `x?/feat` never does. A bare `y` includes the feature `y`
    if the crate has one, else it is the old spelling of `dep:y`.
    """
    implicit = implicit_features(features, optional)
    defined = set(features) | implicit
    turns_on: dict[str, set[str]] = {feature: set() for feature in defined}
    includes: dict[str, set[str]] = {feature: set() for feature in defined}
    for feature in implicit:
        turns_on[feature].add(feature)
    for feature, values in features.items():
        for value in values:
            if value.startswith("dep:"):
                if value[4:] in optional:
                    turns_on[feature].add(value[4:])
            elif "/" in value:
                target = value.partition("/")[0]
                if not target.endswith("?") and target in optional:
                    turns_on[feature].add(target)
                    if target in defined:
                        includes[feature].add(target)   # Cargo turns on a feature named after the dependency too
            elif value in defined:
                includes[feature].add(value)
            elif value in optional:
                turns_on[feature].add(value)

    def reach(start: str) -> set[str]:
        seen, stack, dependencies = {start}, [start], set[str]()
        while stack:   # features may include each other in a cycle
            current = stack.pop()
            dependencies |= turns_on[current]
            for included in includes[current] - seen:
                seen.add(included)
                stack.append(included)
        return dependencies

    reached = {feature: reach(feature) for feature in defined}
    return {name: sorted(feature for feature in defined if name in reached[feature]) for name in sorted(optional)}


# --- one version ------------------------------------------------------------------------------------


def _extra_name(local: str, implicit: Collection[str], activated_by: Mapping[str, Sequence[str]]) -> str | None:
    """The feature that turns an optional dependency on (§7.2): its implicit feature, else the first that does."""
    if local in implicit:
        return local
    return next(iter(activated_by.get(local, ())), None)


def _raw_spec(local: str, package: str | None, req: str) -> str:
    if package is None:
        return f'{local} = "{req}"'
    return f'{local} = {{ package = "{package}", version = "{req}" }}'


def normalize_dependencies(entry: Mapping[str, object]) -> list[NormalizedDependency]:
    """The `deps` of one index line in contract form, in file order (SPEC §7.2)."""
    raw = entry.get("deps")
    declared = [dep for dep in (raw if isinstance(raw, list) else []) if isinstance(dep, Mapping)]
    features = merged_features(entry)
    optional = {dep["name"] for dep in declared if dep.get("optional") is True and isinstance(dep.get("name"), str)}
    activated_by = enabled_by(features, optional)
    implicit = implicit_features(features, optional)

    result: list[NormalizedDependency] = []
    for dep in declared:
        local, req = dep.get("name"), dep.get("req")
        if not isinstance(local, str) or not isinstance(req, str):
            logger.warning("Skipping crates dependency without a name or requirement: %r", dep)
            continue
        package = _text(dep.get("package"))
        kind = dep.get("kind") or "normal"
        dependency_type: DependencyType | None = (
            "optional" if dep.get("optional") is True else _KIND_TYPES.get(kind) if isinstance(kind, str) else None
        )
        if dependency_type is None:
            logger.warning("Skipping crates dependency %s of unknown kind %r", local, kind)
            continue
        try:
            target = dependency_ref(package or local)
        except ValueError:
            logger.warning("Skipping crates dependency with a malformed name %r", package or local)
            continue

        details: dict[str, object] = {}
        dep_features = dep.get("features")
        if isinstance(dep_features, list) and dep_features:
            details["features"] = dep_features
        if dep.get("default_features") is False:
            details["default_features"] = False
        if _text(dep.get("registry")):
            details["registry"] = dep["registry"]
        if dependency_type == "optional" and activated_by.get(local):
            details["enabled_by"] = activated_by[local]
        cfg = _text(dep.get("target"))
        result.append(NormalizedDependency(
            target=target, constraint=req, dependency_type=dependency_type, raw_spec=_raw_spec(local, package, req),
            scope=kind if isinstance(kind, str) else None, alias=local if package else None,
            extra_name=_extra_name(local, implicit, activated_by) if dependency_type == "optional" else None,
            environment_markers={"rust_cfg": cfg} if cfg else None, details=details or None,
        ))
    return result


def _prefix(name: str) -> str:
    """The directory of a crate in the index, without the file: `se/rd`, `3/s`, `2`, `1`."""
    if len(name) <= 2:
        return str(len(name))
    return f"3/{name[0]}" if len(name) == 3 else f"{name[:2]}/{name[2:4]}"


def download_url(dl: str, name: str, version: str, checksum: str) -> str:
    """The `.crate` URL by Cargo's rule for `dl` of config.json."""
    if not any(marker in dl for marker in _DL_MARKERS):
        return f"{dl.rstrip('/')}/{name}/{version}/download"
    for marker, value in (
        ("{crate}", name), ("{version}", version), ("{prefix}", _prefix(name)),
        ("{lowerprefix}", _prefix(name.lower())), ("{sha256-checksum}", checksum),
    ):
        dl = dl.replace(marker, value)
    return dl


def _engines(entry: Mapping[str, object]) -> dict[str, str] | None:
    rust_version = _text(entry.get("rust_version"))
    return {"rust": rust_version} if rust_version else None


def version_from_entry(entry: Mapping[str, object], dl: str) -> NormalizedVersion | None:
    """One index line; None if its version is not a SemVer the crates scheme accepts."""
    name, version, checksum = entry.get("name"), entry.get("vers"), entry.get("cksum")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    try:
        parse_version(version)
    except InvalidVersion:
        logger.warning("Skipping crate %s version %r: not a version Cargo accepts", name, version)
        return None
    artifacts: list[NormalizedArtifact] = []
    if isinstance(checksum, str) and checksum:
        artifacts.append(NormalizedArtifact(
            filename=f"{name}-{version}.crate", artifact_type="crate",
            download_url=download_url(dl, name, version, checksum), checksums={"sha256": checksum},
        ))
    links = entry.get("links")
    return NormalizedVersion(
        version=version,
        status="yanked" if entry.get("yanked") is True else "active",
        release_date=_date(entry.get("pubtime")),
        engines=_engines(entry),
        features=merged_features(entry),
        raw_metadata={"links": links} if links else {},
        artifacts=artifacts,
        dependencies=normalize_dependencies(entry),
    )


# --- the API ----------------------------------------------------------------------------------------


def _license_spdx(raw: str) -> str:
    """Crates published before SPDX expressions separate alternatives with `/`: `MIT/Apache-2.0`."""
    return re.sub(r"\s*/\s*", " OR ", raw)


def _api_license(document: Mapping[str, object], crate: Mapping[str, object]) -> str | None:
    """The license of the default version; the API has no license on the crate itself."""
    listed = document.get("versions")
    versions = [v for v in (listed if isinstance(listed, list) else []) if isinstance(v, Mapping)]
    for key in ("default_version", "max_stable_version"):
        for version in versions:
            if version.get("num") is not None and version.get("num") == crate.get(key):
                return _text(version.get("license"))
    if not versions:
        return None
    return _text(max(versions, key=lambda v: str(v.get("created_at") or "")).get("license"))


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def apply_api_document(package: NormalizedPackage, document: Mapping[str, object]) -> None:
    """Fill the package-level fields from `GET /api/v1/crates/<name>`."""
    crate = document.get("crate")
    crate = crate if isinstance(crate, Mapping) else {}
    keywords = document.get("keywords")
    license_raw = _api_license(document, crate)
    package.description = _text(crate.get("description"))
    package.homepage_url = _text(crate.get("homepage"))
    package.repository_url = _text(crate.get("repository"))
    package.documentation_url = _text(crate.get("documentation"))
    if isinstance(keywords, list):
        package.keywords = [k["id"] for k in keywords if isinstance(k, Mapping) and isinstance(k.get("id"), str)]
    package.downloads_total = _integer(crate.get("downloads"))
    package.downloads_recent = _integer(crate.get("recent_downloads"))
    package.created_at = _date(crate.get("created_at"))
    package.updated_at = _date(crate.get("updated_at"))
    package.license_raw = license_raw
    package.license_spdx = _license_spdx(license_raw) if license_raw else None


# --- the adapter ------------------------------------------------------------------------------------


def _is_missing(response: httpx.Response) -> bool:
    return response.status_code == 404


def _json_object(response: httpx.Response) -> Mapping[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{response.url}: the registry answer is not an object")
    return payload


def _index_lines(response: httpx.Response) -> list[Mapping[str, object]]:
    lines = (json.loads(line) for line in response.text.splitlines() if line.strip())
    return [line for line in lines if isinstance(line, dict)]


def _published_name(lines: Sequence[Mapping[str, object]]) -> str | None:
    name = next((line["name"] for line in lines if isinstance(line.get("name"), str)), None)
    return str(name) if name else None


class CratesSparseAdapter:
    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry
        self.root = registry.base_url.rstrip("/") + "/"
        api_url = registry.config.get("api_url")
        self.api_url = api_url.rstrip("/") if isinstance(api_url, str) and api_url.strip() else None
        settings = get_settings()
        self.timeout_seconds = settings.sync_http_timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds
        self._dl: str | None = None

    def index_url(self, ref: PackageRef) -> str:
        return f"{self.root}{quote(index_path(ref.name), safe='/')}"

    def api_crate_url(self, name: str) -> str | None:
        return f"{self.api_url}/api/v1/crates/{quote(name, safe='')}" if self.api_url else None

    async def _config(self) -> Mapping[str, object]:
        response = await self.get(f"{self.root}config.json", {})
        response.raise_for_status()
        document = _json_object(response)
        if not isinstance(document.get("dl"), str):
            raise ValueError(f"{response.url}: config.json has no `dl`, not a Cargo sparse index")
        return document

    async def _download_base(self) -> str:
        # The same for every crate of the registry: read once per adapter.
        if self._dl is None:
            self._dl = str((await self._config())["dl"])
        return self._dl

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        response = await self.get(self.index_url(ref), {})
        if _is_missing(response):
            return None
        lines = _index_lines(response)
        name = _published_name(lines)
        if name is not None and name != ref.name:
            logger.info("%s in %s is published as %s", ref.name, self.registry.slug, name)
            return None
        dl = await self._download_base()
        versions = [version for line in lines if (version := version_from_entry(line, dl)) is not None]
        return NormalizedPackage(
            ref=ref, registry_id=self.registry.id, display_name=name or _COORDINATES.display(ref),
            revision=response.headers.get("etag"), versions=versions,
        )

    async def fetch_api_fields(self, package: NormalizedPackage) -> bool:
        """Fill the package-level fields from the API. True when the API answered (a crate it does not know included),
        False when there is no API or it failed: the caller tries again later."""
        url = self.api_crate_url(package.ref.name)
        if url is None:
            return False
        try:
            response = await self.get(url, {"Accept": "application/json"}, api=True)
            if _is_missing(response):
                logger.warning("crates.io API does not know %s in %s", package.ref.name, self.registry.slug)
                return True
            apply_api_document(package, _json_object(response))
            return True
        except Exception:  # noqa: BLE001 - one crate failing must not stop the others
            logger.warning("crates.io API failed for %s in %s", package.ref.name, self.registry.slug, exc_info=True)
            return False

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies:
        response = await self.get(self.index_url(ref), {})
        if _is_missing(response):
            return VersionDependencies([], available=False)
        for line in _index_lines(response):
            if line.get("vers") == version:
                return VersionDependencies(dependencies=normalize_dependencies(line), engines=_engines(line))
        return VersionDependencies([], available=False)

    async def exists(self, ref: PackageRef) -> bool:
        # A full GET: the file is what is fetched next, and a CDN in front of the index may not answer HEAD.
        response = await self.get(self.index_url(ref), {})
        if _is_missing(response):
            return False
        name = _published_name(_index_lines(response))
        return name is None or name == ref.name

    async def test_connection(self) -> None:
        await self._config()

    async def canonical_name(self, raw: str) -> str | None:
        """The published spelling of a crate given as `serde-json`, `Serde_Json`, ...; the index knows only the exact
        name, the API finds the crate under any of them. None if there is no such crate or no API to ask."""
        url = self.api_crate_url(raw.strip())
        if url is None:
            return None
        response = await self.get(f"{url}?include=", {"Accept": "application/json"}, api=True)
        if _is_missing(response):
            return None
        crate = _json_object(response).get("crate")
        name = crate.get("name") if isinstance(crate, Mapping) else None
        return name if isinstance(name, str) and name else None

    def change_detector(self) -> ChangeDetector:
        return CratesEtagChangeDetector(self)

    def min_interval_seconds(self) -> float:
        # The index is a CDN; the API has its own limit, kept per request in `get`.
        return 0.0

    async def get(self, url: str, headers: Mapping[str, str], *, api: bool = False) -> httpx.Response:
        """GET with retries on transient errors. 404 and 304 are answers; any other error status is raised.
        An API request waits for its turn in the shared limiter, retries included."""
        attempt = 0
        while True:
            attempt += 1
            try:
                if api:
                    await wait_turn(f"{self.registry.slug}:api", API_INTERVAL_SECONDS)
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
                logger.warning("crates registry retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay, exc.__class__.__name__)
                await asyncio.sleep(delay)


class CratesEtagChangeDetector:
    """A conditional GET of every tracked index file; the ETag is the revision."""

    def __init__(self, adapter: CratesSparseAdapter, concurrency: int | None = None) -> None:
        self.adapter = adapter
        self.concurrency = concurrency or get_settings().daily_update_concurrency

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(package: TrackedPackage) -> tuple[TrackedPackage, Literal["changed", "missing", "same"], str | None]:
            known = package.revision or package.etag
            if known is None:
                return package, "changed", None
            try:
                async with semaphore:
                    response = await self.adapter.get(self.adapter.index_url(package.ref), {"If-None-Match": known})
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
        # Nothing global to remember: every index file answers for itself.
        return ChangeSet(changed=changed, missing=missing, cursor={})
