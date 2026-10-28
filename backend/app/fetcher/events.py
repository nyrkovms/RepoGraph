"""Events from comparing the stored state of a package with its merged sources (SPEC-MULTI-ECOSYSTEM.md §8.3)."""

from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import datetime

from app.ecosystems.base import MergedPackage, VersionScheme
from app.ecosystems.pypi.versions import PyPIVersionScheme
from app.models.enums import PackageEventKind, PackageVersionStatus


@dataclass(slots=True)
class PackageSnapshot:
    latest_version: str | None
    version_statuses: dict[str, PackageVersionStatus]
    vulnerability_withdrawn: dict[str, datetime | None]
    unavailable: bool = False
    # None = not loaded: no source_added events are derived.
    registry_ids: set[str] | None = None                 # package_registries of the package
    version_sources: dict[str, set[str]] | None = None   # version -> registries publishing it (version_sources)
    deprecated_versions: set[str] = field(default_factory=set)


@dataclass(slots=True)
class EventDraft:
    kind: PackageEventKind
    version: str | None = None
    occurred_at: datetime | None = None
    details: dict = field(default_factory=dict)


_PYPI_VERSIONS = PyPIVersionScheme()


def classify_bump(previous: str | None, current: str) -> str | None:
    return _PYPI_VERSIONS.bump(previous, current)


def diff_package(
    before: PackageSnapshot | None,
    after: MergedPackage,
    scheme: VersionScheme,
    latest_version: str | None,
    registry_slugs: Mapping[str, str] | None = None,
    frozen_versions: Set[str] = frozenset(),
) -> list[EventDraft]:
    """Compare stored state with freshly merged metadata and describe what changed. `frozen_versions` are kept by
    sources that were not asked (disabled): their absence from `after` is not a removal."""
    if before is None:
        return [EventDraft(kind=PackageEventKind.tracking_started, details={"latest_version": latest_version})]

    slugs = registry_slugs or {}
    events: list[EventDraft] = []
    if before.unavailable:
        events.append(EventDraft(kind=PackageEventKind.project_restored))

    # A source new to the package: one event for the package, not one per version it publishes.
    new_registries: list[str] = []
    if before.registry_ids is not None:
        new_registries = [r for r in after.registry_revisions if r not in before.registry_ids]
        for registry_id in new_registries:
            events.append(EventDraft(kind=PackageEventKind.source_added, details={"registry": slugs.get(registry_id, registry_id)}))

    incoming = {merged.version.version: merged for merged in after.versions}

    added = [
        merged for name, merged in incoming.items()
        if name not in before.version_statuses and merged.version.status != PackageVersionStatus.removed.value
    ]
    # previous_version follows every new release; the bump is measured against the last stable one,
    # so 3.1.1 -> 3.2.0rc1 -> 3.2.0 reports "pre" and then "minor".
    previous = before.latest_version
    previous_stable = before.latest_version
    for merged in sorted(added, key=lambda item: scheme.sort_key(item.version.version)):
        version = merged.version
        prerelease = scheme.is_prerelease(version.version)
        events.append(
            EventDraft(
                kind=PackageEventKind.release_added,
                version=version.version,
                occurred_at=version.release_date,
                details={
                    "previous_version": previous,
                    "bump": scheme.bump(previous_stable, version.version),
                    "prerelease": prerelease,
                },
            )
        )
        if version.status == PackageVersionStatus.yanked.value:
            events.append(_yanked_event(version.version, version.status_reason))
        previous = version.version
        if not prerelease:
            previous_stable = version.version

    # The same version appearing in one more source is not a new release (§8.3).
    if before.version_sources is not None:
        for name in sorted(incoming, key=scheme.sort_key):
            known = before.version_sources.get(name)
            if known is None:
                continue
            for registry_id in incoming[name].sources:
                if registry_id not in known and registry_id not in new_registries:
                    events.append(EventDraft(kind=PackageEventKind.source_added, version=name,
                                             details={"registry": slugs.get(registry_id, registry_id)}))

    for name in sorted(before.version_statuses, key=scheme.sort_key):
        if name in frozen_versions:
            continue
        old_status = before.version_statuses[name]
        new_merged = incoming.get(name)
        new_status = PackageVersionStatus(new_merged.version.status) if new_merged else PackageVersionStatus.removed
        if old_status == new_status:
            continue
        reason = new_merged.version.status_reason if new_merged else None

        if new_status == PackageVersionStatus.removed:
            events.append(EventDraft(kind=PackageEventKind.release_removed, version=name))
        elif old_status == PackageVersionStatus.removed:
            events.append(EventDraft(kind=PackageEventKind.release_restored, version=name))
            if new_status == PackageVersionStatus.yanked:
                events.append(_yanked_event(name, reason))
        elif new_status == PackageVersionStatus.yanked:
            events.append(_yanked_event(name, reason))
        elif old_status == PackageVersionStatus.yanked:
            events.append(EventDraft(kind=PackageEventKind.release_unyanked, version=name))

    for name in sorted(incoming, key=scheme.sort_key):
        deprecation = incoming[name].version.deprecation
        if deprecation is not None and name not in before.deprecated_versions:
            events.append(EventDraft(kind=PackageEventKind.release_deprecated, version=name, details=dict(deprecation)))

    for vuln in after.package.vulnerabilities or []:
        if vuln.source_id not in before.vulnerability_withdrawn:
            events.append(
                EventDraft(
                    kind=PackageEventKind.vulnerability_added,
                    occurred_at=vuln.published_at,
                    details={
                        "id": vuln.source_id,
                        "aliases": vuln.aliases,
                        "summary": _short_summary(vuln.summary),
                        "fixed_in": vuln.fixed_in,
                    },
                )
            )
        elif vuln.withdrawn_at and before.vulnerability_withdrawn[vuln.source_id] is None:
            events.append(
                EventDraft(
                    kind=PackageEventKind.vulnerability_withdrawn,
                    occurred_at=vuln.withdrawn_at,
                    details={"id": vuln.source_id},
                )
            )

    return events


def _yanked_event(version: str, reason: str | None) -> EventDraft:
    return EventDraft(kind=PackageEventKind.release_yanked, version=version, details={"reason": reason})


def _short_summary(text: str | None, limit: int = 300) -> str | None:
    # PyPI often falls back to the full markdown advisory; keep the first meaningful line only.
    if not text:
        return text
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return None
