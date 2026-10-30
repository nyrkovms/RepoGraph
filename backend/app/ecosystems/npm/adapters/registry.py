"""`npm-registry` source adapter: registry.npmjs.org and other registries that speak the npm registry API
(SPEC-MULTI-ECOSYSTEM.md §6.1, §7.1, §7.2).

The package is read from the full packument (`application/json`, Q19): every version with its dependencies, engines,
`deprecated` and `dist`, `dist-tags`, and `time` with the release date of each version, which the abbreviated
document (`application/vnd.npm.install-v1+json`) lacks. All versions are stored; dependencies are filled in for the
version `latest` points at and fetched lazily from `/<name>/<version>` for the others (Q9).

`test_connection` reads `/-/ping`: answered with 200 by registry.npmjs.org (`{}`), registry.npmmirror.com,
registry.yarnpkg.com, npm.jsr.io and npm.pkg.github.com (empty body), which is what was seen; a registry that answers
differently fails the check with its status, as a wrong `base_url` should.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

import httpx

from app.config import get_settings
from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
    DependencyType,
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
from app.ecosystems.npm.changes import NpmChangesDetector
from app.ecosystems.npm.coordinates import NpmCoordinates, split_name
from app.services.http import make_client
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

# The full packument, for its `time`; its ETag is the package's revision, so the change check asks for it too.
ACCEPT = "application/json"
# The abbreviated one, where only the answer matters.
ACCEPT_ABBREVIATED = "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8"

_COORDINATES = NpmCoordinates()

# package.json field -> our dependency type, in the order the dependencies are listed.
_FIELDS: tuple[tuple[str, DependencyType], ...] = (
    ("dependencies", "runtime"),
    ("devDependencies", "dev"),
    ("peerDependencies", "peer"),
    ("optionalDependencies", "optional"),
)

# Specs that do not name a version of a registry package. A `/` anywhere else (`user/repo#tag`, `./local`)
# is a GitHub shorthand or a path.
_UNRESOLVABLE_PREFIXES = (
    "git+", "git:", "git@", "github:", "gitlab:", "bitbucket:", "gist:", "ssh:",
    "file:", "link:", "http:", "https:", "workspace:", "catalog:", "portal:", "patch:",
)


# --- dependencies -----------------------------------------------------------------------------------


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _is_unresolvable(spec: str) -> bool:
    return spec.lower().startswith(_UNRESOLVABLE_PREFIXES) or "/" in spec


def _ref(name: str) -> PackageRef | None:
    try:
        scope, local = split_name(name)
    except ValueError:
        return None
    return PackageRef(ecosystem="npm", namespace=scope, name=local)


def _dependency(
    name: str, spec: str, dependency_type: DependencyType, scope: str, optional_peer: bool
) -> NormalizedDependency | None:
    declared = _ref(name)
    if declared is None:
        logger.warning("Skipping npm dependency with a malformed name %r", name)
        return None
    details: dict[str, object] = {"optional": True} if optional_peer else {}
    target, constraint, alias = declared, spec, None

    if spec.startswith("npm:"):
        # `"foo": "npm:bar@^1"` installs bar under the name foo; `npm:bar` means bar's `latest`.
        aliased, at, range_ = spec[4:].rpartition("@")
        if not at or not aliased:
            aliased, range_ = spec[4:], ""
        aliased_ref = _ref(aliased)
        if aliased_ref is None:
            details["unresolvable"] = spec
            constraint = ""
        else:
            target, constraint = aliased_ref, range_
            alias = name if aliased_ref != declared else None
    elif _is_unresolvable(spec):
        details["unresolvable"] = spec
        constraint = ""

    return NormalizedDependency(
        target=target, constraint=constraint, dependency_type=dependency_type, raw_spec=f"{name}@{spec}",
        scope=scope, alias=alias, details=details or None,
    )


def normalize_dependencies(manifest: Mapping[str, object]) -> list[NormalizedDependency]:
    """The four dependency fields of a version (packument entry or `/<name>/<version>` document) in contract form.

    npm lists an optional dependency in `dependencies` too; the optional entry wins and is not repeated.
    """
    optional = _mapping(manifest.get("optionalDependencies"))
    peer_meta = _mapping(manifest.get("peerDependenciesMeta"))
    result: list[NormalizedDependency] = []
    for field_name, dependency_type in _FIELDS:
        for name, spec in _mapping(manifest.get(field_name)).items():
            if not isinstance(spec, str):
                continue   # not a spec at all; a version document with garbage in it must not fail its package
            if field_name == "dependencies" and name in optional:
                continue
            optional_peer = field_name == "peerDependencies" and _mapping(peer_meta.get(name)).get("optional") is True
            dependency = _dependency(name, spec, dependency_type, field_name, optional_peer)
            if dependency is not None:
                result.append(dependency)
    return result


def _engines(manifest: Mapping[str, object]) -> dict[str, str] | None:
    """`engines` plus the version's `os`/`cpu` lists (comma-joined, `!` negations kept): all of them are
    requirements on the environment, and readers that only see a version's engines (resolve) need the platform."""
    # Old packages publish `engines` as a list; only a mapping of strings is a constraint we can use.
    engines = {k: v for k, v in _mapping(manifest.get("engines")).items() if isinstance(v, str)}
    for key in ("os", "cpu"):
        values = manifest.get(key)
        if isinstance(values, list) and values and all(isinstance(v, str) for v in values):
            engines[key] = ",".join(values)
    return engines or None


# --- the packument ----------------------------------------------------------------------------------


def _raw_metadata(entry: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in ("os", "cpu"):
        if entry.get(key):
            metadata[key] = entry[key]
    # Both spellings exist; the value is a list of names or `true`. Their contents are in the tarball (SPEC §9.3).
    bundled = entry.get("bundleDependencies") or entry.get("bundledDependencies")
    if bundled:
        metadata["bundleDependencies"] = bundled
    if "_hasShrinkwrap" in entry:
        metadata["_hasShrinkwrap"] = bool(entry["_hasShrinkwrap"])
    return metadata


def _artifact(entry: Mapping[str, object]) -> NormalizedArtifact | None:
    dist = _mapping(entry.get("dist"))
    tarball = dist.get("tarball")
    if not isinstance(tarball, str) or not tarball:
        return None
    checksums: dict[str, str] = {}
    if isinstance(dist.get("shasum"), str) and dist["shasum"]:
        checksums["sha1"] = str(dist["shasum"]).lower()
    if isinstance(dist.get("integrity"), str) and dist["integrity"]:
        checksums["integrity"] = str(dist["integrity"])
    # `unpackedSize` is the size after unpacking, not of the tarball: no size_bytes.
    return NormalizedArtifact(
        filename=unquote(urlsplit(tarball).path.rsplit("/", 1)[-1]), artifact_type="tgz", download_url=tarball,
        checksums=checksums,
    )


def _release_dates(document: Mapping[str, object]) -> dict[str, datetime]:
    """`time`: `{version: ISO date}` next to `created`/`modified`; a date that does not parse is left out."""
    dates: dict[str, datetime] = {}
    for version, value in _mapping(document.get("time")).items():
        if version in ("created", "modified", "unpublished") or not isinstance(value, str):
            continue
        try:
            dates[version] = datetime.fromisoformat(value)
        except ValueError:
            continue
    return dates


def version_from_entry(
    version: str, entry: Mapping[str, object], *, with_dependencies: bool, release_date: datetime | None = None
) -> NormalizedVersion:
    """One version of the packument; its release date comes from the packument's `time`."""
    deprecated = entry.get("deprecated")
    artifact = _artifact(entry)
    return NormalizedVersion(
        version=version,
        # npm unpublish removes a version: it disappears from the packument, which the pipeline turns into removed.
        status="active",
        release_date=release_date,
        deprecation={"message": deprecated} if isinstance(deprecated, str) and deprecated else None,
        engines=_engines(entry),
        raw_metadata=_raw_metadata(entry),
        artifacts=[artifact] if artifact else [],
        dependencies=normalize_dependencies(entry) if with_dependencies else None,
    )


def package_from_packument(
    ref: PackageRef, registry_id: str, document: Mapping[str, object], revision: str | None
) -> NormalizedPackage:
    tags = {tag: target for tag, target in _mapping(document.get("dist-tags")).items() if isinstance(target, str)}
    latest = tags.get("latest")
    dates = _release_dates(document)
    name = document.get("name")
    return NormalizedPackage(
        ref=ref, registry_id=registry_id,
        display_name=name if isinstance(name, str) and name else _COORDINATES.display(ref),
        revision=revision,
        # The dependencies of the other versions are fetched lazily.
        versions=[
            version_from_entry(version, entry, with_dependencies=version == latest, release_date=dates.get(version))
            for version, entry in _mapping(document.get("versions")).items()
            if isinstance(entry, Mapping)
        ],
        dist_tags=tags,
    )


# --- the adapter ------------------------------------------------------------------------------------


def _is_missing(response: httpx.Response) -> bool:
    return response.status_code == 404


def _json_object(response: httpx.Response) -> Mapping[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{response.url}: the registry answer is not an object")
    return payload


class NpmRegistryAdapter:
    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry
        self.root = registry.base_url.rstrip("/") + "/"
        settings = get_settings()
        self.timeout_seconds = settings.sync_http_timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds

    def package_url(self, ref: PackageRef) -> str:
        """`@scope%2fname`, the way npm clients ask for a scoped package."""
        if ref.namespace:
            return f"{self.root}@{quote(ref.namespace, safe='')}%2f{quote(ref.name, safe='')}"
        return f"{self.root}{quote(ref.name, safe='')}"

    def version_url(self, ref: PackageRef, version: str) -> str:
        return f"{self.package_url(ref)}/{quote(version, safe='')}"

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        response = await self.get(self.package_url(ref), {"Accept": ACCEPT})
        if _is_missing(response):
            return None
        return package_from_packument(ref, self.registry.id, _json_object(response), response.headers.get("etag"))

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies:
        response = await self.get(self.version_url(ref, version), {"Accept": "application/json"})
        if _is_missing(response):
            return VersionDependencies([], available=False)
        document = _json_object(response)
        return VersionDependencies(dependencies=normalize_dependencies(document), engines=_engines(document))

    async def exists(self, ref: PackageRef) -> bool:
        # A full GET: not every registry in front of npm answers HEAD, and the packument is what is fetched next.
        return not _is_missing(await self.get(self.package_url(ref), {"Accept": ACCEPT_ABBREVIATED}))

    async def test_connection(self) -> None:
        (await self.get(f"{self.root}-/ping", {})).raise_for_status()

    def change_detector(self) -> ChangeDetector:
        changes_url = self.registry.config.get("changes_url")
        if isinstance(changes_url, str) and changes_url:
            # The replication feed of registry.npmjs.org; full checks go packument by packument with the ETag.
            return NpmChangesDetector(self.registry, changes_url, reconciler=NpmEtagChangeDetector(self))
        return NpmEtagChangeDetector(self)

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
                logger.warning("npm registry retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay, exc.__class__.__name__)
                await asyncio.sleep(delay)


class NpmEtagChangeDetector:
    """A conditional GET of every tracked packument; the ETag is the revision. For registries without a feed."""

    def __init__(self, adapter: NpmRegistryAdapter, concurrency: int | None = None) -> None:
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
                    response = await self.adapter.get(
                        self.adapter.package_url(package.ref), {"Accept": ACCEPT, "If-None-Match": known}
                    )
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
        # Nothing global to remember: every packument answers for itself.
        return ChangeSet(changed=changed, missing=missing, cursor={})
