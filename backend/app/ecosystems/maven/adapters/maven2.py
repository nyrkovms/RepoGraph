"""`maven2` source adapter: any repository in the Maven 2 layout - Maven Central, Google Maven, the Gradle Plugin
Portal, JBoss, corporate Nexus and Artifactory (SPEC-MULTI-ECOSYSTEM.md §6.1, §7.1-§7.4).

A package is `<group path>/<artifact>/maven-metadata.xml` (its versions in publication order, `<release>`, `<latest>`,
`<lastUpdated>`), a version is `<version>/<artifact>-<version>.pom` plus its files. The layout has no description of the
repository, no feed of changes and no checksums in the metadata, and only the POM's `Last-Modified` dates a version:
the revision of a package is the ETag of its metadata (else `lastUpdated`, else a hash of the versions), and checksums
and sizes stay empty (§7.1). Only the latest version is dated at once, from the `Last-Modified` of the POM read anyway;
the others are dated in the background (`release_date`, tasks/maven_dates.py) like the crates.io API fields (Q22):
a HEAD for each of hundreds of versions would make every lazy fetch during a resolve take minutes.

The dependencies of the latest version are read at once, through its effective model (parents and BOMs, §7.4), and
lazily for the others (`fetch_version_dependencies`). A parent or BOM may live in another repository of the profile:
it is asked from there through `lookup`, and the POMs read from this repository are handed back as documents.
`<repositories>`, SNAPSHOT versions and `.module` files are not read (§6.2, §7.4).
"""

import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ElementTree
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, cast

import httpx

from app.config import get_settings
from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
    FetchedDocument,
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
from app.ecosystems.maven.coordinates import MavenCoordinates, file_path, metadata_path, pom_path
from app.ecosystems.maven.pom import Dependency, EffectiveModel, Pom, PomError, effective_model, parse_pom
from app.ecosystems.maven.versions import MavenVersionScheme
from app.services.http import make_client
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error

logger = logging.getLogger(__name__)

_COORDINATES = MavenCoordinates()
_SCHEME = MavenVersionScheme()
# Maven repositories answer "no such artifact" with 404, and 410 for what they know was removed.
_MISSING = (404, 410)
_TEST_ARTIFACT = "org/apache/maven/maven-artifact/maven-metadata.xml"
_POM_CONTENT_TYPE = "application/xml"
# Gradle writes this into the POM of everything it publishes together with a `.module` file.
_GRADLE_MARKER = re.compile(rb"<!--\s*do_not_remove:\s*published-with-gradle-metadata\s*-->")
# Packagings whose main file is a plain `.jar`; any other packaging (`war`, `aar`, ...) is its own extension.
_JAR_PACKAGINGS = frozenset({"jar", "bundle", "maven-plugin", "eclipse-plugin", "takari-jar", "hk2-jar"})
_DEPENDENCY_TYPES: Mapping[str, Literal["runtime", "dev", "provided"]] = {
    "compile": "runtime", "runtime": "runtime", "test": "dev", "provided": "provided", "system": "provided",
}


# --- the POM cache --------------------------------------------------------------------------------------------


class _PomCache:
    """Raw parent and BOM POMs by (base url, path). A published POM never changes, so there is no TTL; the bound only
    keeps a long-lived worker small. One daily run reads `spring-boot-dependencies` once, not once per artifact."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._items: OrderedDict[tuple[str, str], tuple[bytes, str | None]] = OrderedDict()

    def get(self, key: tuple[str, str]) -> tuple[bytes, str | None] | None:
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, key: tuple[str, str], content: bytes, etag: str | None) -> None:
        self._items[key] = (content, etag)
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


_POM_CACHE = _PomCache(512)


# --- maven-metadata.xml ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metadata:
    versions: list[str]
    release: str | None
    latest: str | None
    last_updated: str | None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _text(element: ElementTree.Element | None, name: str) -> str | None:
    found = _child(element, name)
    return (found.text or "").strip() or None if found is not None else None


def parse_metadata(content: bytes | str, url: str) -> Metadata:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{url}: not a maven-metadata.xml: {exc}") from None
    if _local(root.tag) != "metadata":
        raise ValueError(f"{url}: not a maven-metadata.xml: the root element is {_local(root.tag)!r}")
    versioning = _child(root, "versioning")
    listed = _child(versioning, "versions")
    versions = [text for v in (listed if listed is not None else []) if _local(v.tag) == "version"
                if (text := (v.text or "").strip())]
    return Metadata(
        versions=versions, release=_text(versioning, "release"), latest=_text(versioning, "latest"),
        last_updated=_text(versioning, "lastUpdated"),
    )


def kept_versions(raw: Sequence[str], ref: PackageRef | None = None) -> list[str]:
    """Document order, de-duplicated, without SNAPSHOTs (snapshot repositories are not connected, §6.2) and without
    what the scheme rejects. `ref` is given to log what is dropped."""
    kept: list[str] = []
    for version in dict.fromkeys(raw):
        if version.endswith("-SNAPSHOT"):
            continue
        try:
            _SCHEME.validate(version)
        except InvalidVersion:
            if ref is not None:
                logger.warning("Skipping Maven artifact %s version %r: not a version Maven accepts",
                               _COORDINATES.display(ref), version)
            continue
        kept.append(version)
    return kept


def metadata_revision(etag: str | None, metadata: Metadata, versions: Collection[str]) -> str:
    """The ETag as sent (quotes kept), else `lastUpdated:<value>`, else a hash of the sorted versions."""
    if etag:
        return etag
    if metadata.last_updated:
        return f"lastUpdated:{metadata.last_updated}"
    return hashlib.sha256("\n".join(sorted(versions)).encode()).hexdigest()


def _looks_like_etag(revision: str) -> bool:
    return revision.startswith(('"', 'W/"'))


def pick_latest(release: str | None, versions: Sequence[str]) -> str | None:
    """`<release>` when it is among the kept versions, else the highest release, else the highest prerelease."""
    if release is not None and release in versions:
        return release
    releases = [v for v in versions if not _SCHEME.is_prerelease(v)]
    return max(releases or list(versions), key=_SCHEME.sort_key, default=None)


# --- dependencies and package fields --------------------------------------------------------------------------


def dependencies_of(model: EffectiveModel) -> list[NormalizedDependency]:
    result: list[NormalizedDependency] = []
    for dep in model.dependencies:
        try:
            if "${" in dep.group or "${" in dep.artifact:
                raise ValueError("an unresolved property")
            target = _COORDINATES.parse(f"{dep.group}:{dep.artifact}")
        except ValueError:
            logger.warning("Skipping Maven dependency %s:%s of %s:%s:%s: not a groupId and artifactId",
                           dep.group, dep.artifact, model.group, model.artifact, model.version)
            continue
        scope = dep.scope or "compile"
        version = dep.version or ""
        result.append(NormalizedDependency(
            target=target, constraint=version, scope=scope,
            dependency_type="optional" if dep.optional else _DEPENDENCY_TYPES.get(scope, "runtime"),
            raw_spec=f"{dep.group}:{dep.artifact}:{version}" + (f":{scope}" if scope != "compile" else ""),
            details=_dependency_details(dep),
        ))
    return result


def _dependency_details(dep: Dependency) -> dict[str, object] | None:
    details: dict[str, object] = {}
    if dep.exclusions:
        details["exclusions"] = [{"group": e.group, "artifact": e.artifact} for e in dep.exclusions]
    if dep.type != "jar":
        details["type"] = dep.type
    if dep.classifier:
        details["classifier"] = dep.classifier
    if dep.unresolved:
        details["unresolved_property"] = list(dep.unresolved)
    return details or None


def _managed_entry(dep: Dependency) -> dict[str, object]:
    entry: dict[str, object] = {"group": dep.group, "artifact": dep.artifact, "version": dep.version,
                                "scope": dep.scope, "type": dep.type if dep.type != "jar" else None,
                                "classifier": dep.classifier}
    return {key: value for key, value in entry.items() if value is not None}


def _raw_metadata(model: EffectiveModel, gradle: bool) -> dict[str, object]:
    raw: dict[str, object] = {"packaging": model.packaging, "managed": [_managed_entry(d) for d in model.managed]}
    if model.problems:
        raw["problems"] = list(model.problems)
    if gradle:
        raw["gradle_metadata"] = True
    return raw


def _deprecation(model: EffectiveModel) -> dict[str, object] | None:
    relocation = model.relocation
    if relocation is None:
        return None
    target = f"{relocation.group or model.group}:{relocation.artifact or model.artifact}"
    if relocation.version:
        target += f":{relocation.version}"
    return {"message": relocation.message or "relocated", "alternative": target}


def _authors(model: EffectiveModel) -> list[NormalizedAuthor] | None:
    authors = [NormalizedAuthor(name, email, None, "author") for name, email in model.developers if name]
    return authors or None


def _main_extension(packaging: str) -> str:
    return "jar" if packaging in _JAR_PACKAGINGS else packaging


def _release_date(response: httpx.Response) -> datetime | None:
    value = response.headers.get("last-modified")
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --- parents and BOMs -----------------------------------------------------------------------------------------

type _Fetch = Callable[[str], Awaitable[FetchedDocument | None]]


class _PomLoader:
    """`load` of the effective model for one fetch: this repository first, then the other repositories the lookup
    names for the parent's group. The POMs read from this repository are collected as documents."""

    def __init__(self, adapter: "Maven2Adapter", lookup: SourceLookup | None) -> None:
        self.adapter = adapter
        self.lookup = lookup
        self.documents: dict[str, FetchedDocument] = {}

    async def __call__(self, group: str, artifact: str, version: str) -> Pom | None:
        try:
            if "${" in version:
                raise ValueError("an unresolved property")
            ref = _COORDINATES.parse(f"{group}:{artifact}")
            path = pom_path(ref, version)
        except ValueError:
            logger.warning("Not loading %s:%s:%s: not Maven coordinates", group, artifact, version)
            return None
        own = await self._own(path)
        if own is not None:
            pom = self._parse(own, f"{group}:{artifact}:{version}")
            if pom is not None:
                self.documents.setdefault(path, own)
                return pom
        for other in self._others(ref):
            document = await self._other(other, path)
            if document is not None and (pom := self._parse(document, f"{group}:{artifact}:{version}")) is not None:
                return pom
        return None

    def add(self, path: str, content: bytes, etag: str | None) -> None:
        """The POM of the version itself, which `fetch_*` has already read."""
        self.documents.setdefault(path, _document(path, content, etag))

    async def _own(self, path: str) -> FetchedDocument | None:
        key = (self.adapter.root, path)
        cached = _POM_CACHE.get(key)
        if cached is None:
            response = await self.adapter.get(self.adapter.root + path)
            if _is_missing(response):
                return None
            cached = (response.content, response.headers.get("etag"))
            _POM_CACHE.put(key, *cached)
        return _document(path, *cached)

    async def _other(self, adapter: object, path: str) -> FetchedDocument | None:
        fetch = cast(_Fetch, getattr(adapter, "fetch_document"))
        base = _root_of(adapter)
        cached = _POM_CACHE.get((base, path)) if base is not None else None
        if cached is not None:
            return _document(path, *cached)
        document = await fetch(path)
        if document is not None and base is not None:
            _POM_CACHE.put((base, path), document.content, document.etag)
        return document

    def _others(self, ref: PackageRef) -> list[object]:
        if self.lookup is None:
            return []
        return [a for a in self.lookup.adapters_for(ref)
                if a.registry.id != self.adapter.registry.id and hasattr(a, "fetch_document")]

    @staticmethod
    def _parse(document: FetchedDocument, label: str) -> Pom | None:
        try:
            return parse_pom(document.content)
        except PomError as exc:
            logger.warning("Unreadable POM of %s: %s", label, exc)
            return None


def _document(path: str, content: bytes, etag: str | None) -> FetchedDocument:
    return FetchedDocument(path=path, content=content, content_type=_POM_CONTENT_TYPE, etag=etag)


def _root_of(adapter: object) -> str | None:
    registry = getattr(adapter, "registry", None)
    base_url = getattr(registry, "base_url", None)
    return base_url.rstrip("/") + "/" if isinstance(base_url, str) else None


def _is_missing(response: httpx.Response) -> bool:
    return response.status_code in _MISSING


# --- the adapter ----------------------------------------------------------------------------------------------


class Maven2Adapter:
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

    def metadata_url(self, ref: PackageRef) -> str:
        return self.root + metadata_path(ref)

    def pom_url(self, ref: PackageRef, version: str) -> str:
        return self.root + pom_path(ref, version)

    def file_url(self, ref: PackageRef, version: str, extension: str) -> str:
        return self.root + file_path(ref, version, extension)

    # --- SourceAdapter ---

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        url = self.metadata_url(ref)
        response = await self.get(url)
        if _is_missing(response):
            return None
        metadata = parse_metadata(response.content, url)
        versions = kept_versions(metadata.versions, ref)
        latest = pick_latest(metadata.release, versions)
        read = await self._read_version(ref, latest, _PomLoader(self, lookup)) if latest is not None else None
        model, content, latest_date = read if read is not None else (None, b"", None)
        gradle = _GRADLE_MARKER.search(content) is not None

        normalized: list[NormalizedVersion] = []
        for version in versions:
            # Only the latest version has its POM read at once; the others are lazy and their packaging is unknown.
            top = model if version == latest else None
            normalized.append(NormalizedVersion(
                version=version, status="active", release_date=latest_date if version == latest else None,
                raw_metadata=_raw_metadata(top, gradle) if top is not None else {},
                artifacts=self._artifacts(ref, version, top, gradle and top is not None),
                dependencies=dependencies_of(top) if top is not None else None,
            ))
        package = NormalizedPackage(
            ref=ref, registry_id=self.registry.id, display_name=_COORDINATES.display(ref),
            revision=metadata_revision(response.headers.get("etag"), metadata, versions), versions=normalized,
            dist_tags={"latest": latest} if latest is not None and latest == metadata.release else None,
        )
        if model is not None:
            package.description = model.description
            package.homepage_url = model.url
            package.repository_url = model.scm_url
            package.license_raw = model.license_raw
            package.license_spdx = model.license_spdx
            package.authors = _authors(model)
            package.deprecation = _deprecation(model)
        return package

    async def fetch_version_dependencies(
        self, ref: PackageRef, version: str, lookup: SourceLookup,
    ) -> VersionDependencies:
        loader = _PomLoader(self, lookup)
        read = await self._read_version(ref, version, loader)
        if read is None:
            return VersionDependencies([], available=False)
        model, _, _ = read
        return VersionDependencies(dependencies_of(model), documents=list(loader.documents.values()))

    async def fetch_document(self, path: str) -> FetchedDocument | None:
        """A document by its path relative to the repository root (the POM of a parent, asked by another repository)."""
        response = await self.get(self.root + path.lstrip("/"))
        if _is_missing(response):
            return None
        return FetchedDocument(
            path=path, content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            etag=response.headers.get("etag"),
        )

    async def exists(self, ref: PackageRef) -> bool:
        return not _is_missing(await self.get(self.metadata_url(ref)))

    async def test_connection(self) -> None:
        # Google Maven does not hold maven-artifact: a repository that answers 404 is still a repository.
        await self.get(self.root + _TEST_ARTIFACT)

    def change_detector(self) -> ChangeDetector:
        return Maven2ChangeDetector(self)

    def min_interval_seconds(self) -> float:
        return 0.0

    # --- pieces ---

    async def release_date(self, ref: PackageRef, version: str) -> datetime | None:
        """The `Last-Modified` of the version's POM (a HEAD); None when the POM or the header is missing. Errors of the
        source propagate: the caller tries again later."""
        response = await self.head(self.pom_url(ref, version))
        return None if _is_missing(response) else _release_date(response)

    async def _read_version(
        self, ref: PackageRef, version: str, loader: _PomLoader,
    ) -> tuple[EffectiveModel, bytes, datetime | None] | None:
        """The effective model of a version, the bytes of its POM and its date; None if the POM is missing or
        unreadable. Other errors are source errors (§6.4) and propagate."""
        label = f"{_COORDINATES.display(ref)}@{version}"
        response = await self.get(self.pom_url(ref, version))
        if _is_missing(response):
            logger.warning("%s has no POM in %s", label, self.registry.slug)
            return None
        try:
            pom = parse_pom(response.content)
            loader.add(pom_path(ref, version), response.content, response.headers.get("etag"))
            model = await effective_model(pom, loader)
        except PomError as exc:
            logger.warning("Unreadable POM of %s in %s: %s", label, self.registry.slug, exc)
            return None
        return model, response.content, _release_date(response)

    def _artifacts(
        self, ref: PackageRef, version: str, model: EffectiveModel | None, gradle: bool,
    ) -> list[NormalizedArtifact]:
        artifacts = [self._artifact(ref, version, "pom", has_metadata_file=gradle)]
        if model is None:
            # Without their POM the packaging of the other versions is unknown: `jar` is the usual one.
            artifacts.append(self._artifact(ref, version, "jar"))
        elif model.packaging != "pom":
            artifacts.append(self._artifact(ref, version, _main_extension(model.packaging)))
        return artifacts

    def _artifact(self, ref: PackageRef, version: str, extension: str, has_metadata_file: bool = False) -> NormalizedArtifact:
        return NormalizedArtifact(
            filename=f"{ref.name}-{version}.{extension}", artifact_type=extension,
            download_url=self.file_url(ref, version, extension), has_metadata_file=has_metadata_file,
        )

    async def get(self, url: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
        """GET with retries on transient errors. 404, 410 and 304 are answers; any other error status is raised."""
        return await self._request("GET", url, headers)

    async def head(self, url: str) -> httpx.Response:
        """HEAD with the same policy as `get`."""
        return await self._request("HEAD", url, None)

    async def _request(self, method: str, url: str, headers: Mapping[str, str] | None) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with make_client(timeout=self.timeout_seconds) as client:
                    response = await client.request(method, url, headers=dict(headers or {}), follow_redirects=True)
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
                logger.warning("maven2 retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay,
                               exc.__class__.__name__)
                await asyncio.sleep(delay)


class Maven2ChangeDetector:
    """The metadata of every tracked package, asked with `If-None-Match` while the stored revision is an ETag; a full
    GET otherwise (`lastUpdated:` and hash revisions)."""

    def __init__(self, adapter: Maven2Adapter, concurrency: int | None = None) -> None:
        self.adapter = adapter
        self.concurrency = concurrency or get_settings().daily_update_concurrency

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(package: TrackedPackage) -> tuple[TrackedPackage, Literal["changed", "missing", "same"], str | None]:
            known = package.revision
            if known is None:
                return package, "changed", None
            url = self.adapter.metadata_url(package.ref)
            try:
                async with semaphore:
                    response = await self.adapter.get(
                        url, {"If-None-Match": known} if _looks_like_etag(known) else None)
                if _is_missing(response):
                    # Already known to be unavailable: nothing new to report until it comes back.
                    return package, "same" if package.unavailable else "missing", None
                revision = known if response.status_code == 304 else self._revision(response, url)
            except Exception:  # noqa: BLE001 - one package failing must not fail the whole source for the day
                logger.warning("Change check of %s in %s failed", _COORDINATES.display(package.ref),
                               self.adapter.registry.slug, exc_info=True)
                return package, "same", None
            if revision == known:
                return package, "changed" if package.unavailable else "same", revision
            return package, "changed", revision

        changed: list[tuple[TrackedPackage, str | None]] = []
        missing: list[TrackedPackage] = []
        for package, outcome, revision in await asyncio.gather(*(one(package) for package in tracked)):
            if outcome == "changed":
                changed.append((package, revision))
            elif outcome == "missing":
                missing.append(package)
        # Nothing global to remember: every package answers for itself.
        return ChangeSet(changed=changed, missing=missing, cursor={})

    @staticmethod
    def _revision(response: httpx.Response, url: str) -> str:
        etag: str | None = response.headers.get("etag")
        if etag:
            return etag
        metadata = parse_metadata(response.content, url)
        return metadata_revision(None, metadata, kept_versions(metadata.versions))
