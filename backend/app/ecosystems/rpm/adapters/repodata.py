"""`rpm-repodata` source adapter (SPEC-MULTI-ECOSYSTEM.md §6.1, §6.4, §7.1-§7.2, §8): a thin client of rpm-worker,
which holds the repository metadata (§2.2).

A package is every build of a binary package name in the source, in every arch of `config.arch`. A version is an EVR;
its builds per arch are its artifacts. A build is `superseded` (Q3: no event) when a newer build competes with it in
the source: same module stream (non-modular builds are a stream of their own), and the same arch or noarch on either
side (dnf would take the newer one); a version is superseded when all its builds are. A build that left the source
is superseded too (the plugin's `vanished_status`). Dependencies are capabilities: Requires → runtime,
Recommends/Suggests/Supplements/Enhances → optional with `details.kind`; the capability is the target's name (§7.2),
and a dependency only some arches have is marked `environment_markers.rpm_arch`.
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from app.ecosystems.base import (
    ChangeDetector,
    ChangeSet,
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
from app.ecosystems.rpm import client
from app.ecosystems.rpm.coordinates import RpmCoordinates
from app.ecosystems.rpm.versions import RpmVersionScheme

logger = logging.getLogger(__name__)

_COORDINATES = RpmCoordinates()
_SCHEME = RpmVersionScheme()
_WEAK = ("recommends", "suggests", "supplements", "enhances")
_OPERATORS = ("<=", ">=", "=", "<", ">")
Build = Mapping[str, Any]


def split_capability(text: str) -> tuple[str, str]:
    """`openssl-libs(x86-64) = 1:3.0.7-27.el9` → (`openssl-libs(x86-64)`, `= 1:3.0.7-27.el9`); a rich dependency
    (`(a if b)`) stays whole with no constraint."""
    if text.startswith("("):
        return text, ""
    for operator in _OPERATORS:
        marker = f" {operator} "
        if marker in text:
            name, _, evr = text.partition(marker)
            return name.strip(), f"{operator} {evr.strip()}"
    return text.strip(), ""


def _dependencies(namespace: str, builds: Sequence[Build]) -> list[NormalizedDependency]:
    """The union over the arches of one version; a dependency some arches lack carries those that have it."""
    arches = sorted({str(b["arch"]) for b in builds})
    seen: dict[tuple[str, str], set[str]] = {}
    order: list[tuple[str, str]] = []
    for build in builds:
        for kind in ("requires", *_WEAK):
            for capability in build.get(kind) or []:
                key = (kind, str(capability))
                if key not in seen:
                    seen[key] = set()
                    order.append(key)
                seen[key].add(str(build["arch"]))
    result: list[NormalizedDependency] = []
    for kind, capability in order:
        name, constraint = split_capability(capability)
        markers = ({"rpm_arch": sorted(seen[(kind, capability)])}
                   if len(arches) > 1 and seen[(kind, capability)] != set(arches) else None)
        result.append(NormalizedDependency(
            target=PackageRef("rpm", namespace, name), constraint=constraint, raw_spec=capability,
            dependency_type="runtime" if kind == "requires" else "optional", scope=kind,
            details=None if kind == "requires" else {"kind": kind}, environment_markers=markers,
        ))
    return result


def _config_hash(registry: RegistryConfig) -> str:
    payload = json.dumps(client.source_payload(registry), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _release_date(build: Build) -> datetime | None:
    stamp = build.get("buildtime")
    return datetime.fromtimestamp(int(stamp), UTC) if isinstance(stamp, int) and stamp > 0 else None


def _nevra(build: Build) -> str:
    return f"{build['name']}-{build['evr']}.{build['arch']}"


def _current(builds: Sequence[Build], streams: Mapping[str, Any]) -> set[str]:
    """NEVRAs no other build of the source supersedes: within its module stream, the newest for some arch it covers,
    counting noarch builds as covering every arch."""
    def stream(build: Build) -> str:
        found = streams.get(_nevra(build)) or []
        return ",".join(sorted(str(s) for s in found))

    def arches(build: Build) -> set[str]:
        return {str(a) for a in build.get("repo_arches") or []} if build["arch"] == "noarch" else {str(build["arch"])}

    current: set[str] = set()
    for build in builds:
        for arch in arches(build):
            rivals = [b for b in builds if stream(b) == stream(build) and arch in arches(b)]
            if all(_SCHEME.sort_key(str(b["evr"])) <= _SCHEME.sort_key(str(build["evr"])) for b in rivals):
                current.add(_nevra(build))
                break
    return current


class RepodataAdapter:
    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry

    def _base(self, arch: str) -> str:
        config = self.registry.config
        url = self.registry.base_url.replace("$basearch", arch).replace(
            "$releasever", str(config.get("releasever") or config.get("release") or ""))
        return url if url.endswith("/") else url + "/"

    def _artifact(self, build: Build) -> NormalizedArtifact:
        location = str(build.get("location") or "")
        checksum = build.get("checksum") or {}
        arch = str(build["arch"])
        repo_arch = (build.get("repo_arches") or [arch])[0]
        return NormalizedArtifact(
            filename=location.rsplit("/", 1)[-1], artifact_type="rpm",
            download_url=self._base(str(repo_arch)) + location if location else None,
            checksums={str(checksum["type"]): str(checksum["value"])} if checksum else {},
            size_bytes=build.get("size"), arch=arch,
        )

    def normalize(self, ref: PackageRef, answer: Mapping[str, Any]) -> NormalizedPackage:
        builds: list[Build] = list(answer.get("builds") or [])
        streams: Mapping[str, Any] = answer.get("modules") or {}
        current_builds = _current(builds, streams)
        by_evr: dict[str, list[Build]] = {}
        for build in builds:
            by_evr.setdefault(str(build["evr"]), []).append(build)
        versions: list[NormalizedVersion] = []
        for evr in sorted(by_evr, key=_SCHEME.sort_key):
            group = by_evr[evr]
            current = any(_nevra(b) in current_builds for b in group)
            nevras = [f"{b['name']}-{b['evr']}.{b['arch']}" for b in group]
            module = sorted({s for n in nevras for s in streams.get(n, [])})
            raw: dict[str, Any] = {"provides": sorted({p for b in group for p in b.get("provides") or []}),
                                   "sourcerpm": group[0].get("sourcerpm"), "arches": sorted(str(b["arch"]) for b in group)}
            if module:
                raw["modules"] = module
            versions.append(NormalizedVersion(
                version=evr, status="active" if current else "superseded",
                status_reason=None if current else "a newer build of the same arch is in the source",
                release_date=max((d for b in group if (d := _release_date(b)) is not None), default=None),
                raw_metadata=raw, artifacts=[self._artifact(b) for b in group],
                dependencies=_dependencies(ref.namespace, group),
            ))
        latest = max(builds, key=lambda b: _SCHEME.sort_key(str(b["evr"]))) if builds else {}
        return NormalizedPackage(
            ref=ref, registry_id=self.registry.id, display_name=_COORDINATES.display(ref),
            revision=str(answer.get("revision") or "") or None, versions=versions,
            description=latest.get("description") or latest.get("summary"), homepage_url=latest.get("url") or None,
            license_raw=latest.get("license") or None,
        )

    # --- SourceAdapter ---

    def _serves(self, ref: PackageRef) -> bool:
        namespace = self.registry.config.get("namespace")
        return not namespace or namespace == ref.namespace

    async def fetch_package(self, ref: PackageRef, lookup: SourceLookup) -> NormalizedPackage | None:
        if not self._serves(ref):
            return None
        answer = await client.fetch_package(self.registry, ref.name)
        return self.normalize(ref, answer) if answer else None

    async def fetch_version_dependencies(self, ref: PackageRef, version: str, lookup: SourceLookup) -> VersionDependencies:
        # Every version is read with the package (the worker has the whole repository); asked again, it answers fast.
        package = await self.fetch_package(ref, lookup)
        found = next((v for v in package.versions if v.version == version), None) if package else None
        if found is None or found.dependencies is None:
            return VersionDependencies([], available=False)
        return VersionDependencies(found.dependencies)

    async def exists(self, ref: PackageRef) -> bool:
        return self._serves(ref) and await client.fetch_package(self.registry, ref.name) is not None

    async def test_connection(self) -> None:
        # A repository that answers (even with no repomd.xml there) is reachable; authorization errors raise.
        await client.test_connection(self.registry)

    def change_detector(self) -> ChangeDetector:
        return RpmChangeDetector(self)

    def min_interval_seconds(self) -> float:
        return 0.0


class RpmChangeDetector:
    """One call to the worker per source: it refreshes the repository (conditionally on repomd.xml) and compares every
    tracked package's builds with its stored revision (§8)."""

    def __init__(self, adapter: RepodataAdapter) -> None:
        self.adapter = adapter

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        mine = [p for p in tracked if self.adapter._serves(p.ref)]
        names = {p.ref.name: p for p in mine}
        # §8.1, after Pulp: an unchanged repository skips the comparison, except after a change of the source's
        # configuration (base_url, arches, auth), which the cursor remembers.
        config = _config_hash(self.adapter.registry)
        revisions = cursor.get("revisions")
        known = revisions if isinstance(revisions, dict) and cursor.get("config") == config else None
        answer = await client.detect_changes(self.adapter.registry, {n: p.revision for n, p in names.items()},
                                             known)
        changed_names: Mapping[str, str] = answer.get("changed") or {}
        missing_names = set(answer.get("missing") or [])
        changed: list[tuple[TrackedPackage, str | None]] = []
        missing: list[TrackedPackage] = []
        for name, package in names.items():
            outcome: Literal["changed", "missing", "same"]
            if name in missing_names:
                outcome = "same" if package.unavailable else "missing"
            elif name in changed_names or package.unavailable:
                outcome = "changed"
            else:
                outcome = "same"
            if outcome == "changed":
                changed.append((package, changed_names.get(name, package.revision)))
            elif outcome == "missing":
                missing.append(package)
        return ChangeSet(changed=changed, missing=missing,
                         cursor={"revisions": answer.get("revisions") or {}, "config": config})
