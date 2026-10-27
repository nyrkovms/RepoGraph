from dataclasses import dataclass, field
from datetime import datetime

from packaging.version import InvalidVersion, Version

from app.connectors.base import NormalizedPackage
from app.models.enums import PackageEventKind, PackageVersionStatus


@dataclass(slots=True)
class PackageSnapshot:
    latest_version: str | None
    version_statuses: dict[str, PackageVersionStatus]
    vulnerability_withdrawn: dict[str, datetime | None]
    unavailable: bool = False


@dataclass(slots=True)
class EventDraft:
    kind: PackageEventKind
    version: str | None = None
    occurred_at: datetime | None = None
    details: dict = field(default_factory=dict)


def classify_bump(previous: str | None, current: str) -> str | None:
    if previous is None:
        return None
    try:
        prev_ver = Version(previous)
        cur_ver = Version(current)
    except InvalidVersion:
        return "other"

    if cur_ver.is_prerelease or cur_ver.is_devrelease:
        return "pre"
    if cur_ver <= prev_ver:
        return "other"

    prev_release = prev_ver.release + (0, 0, 0)
    cur_release = cur_ver.release + (0, 0, 0)
    if cur_ver.epoch != prev_ver.epoch or cur_release[0] != prev_release[0]:
        return "major"
    if cur_release[1] != prev_release[1]:
        return "minor"
    if cur_release == prev_release:
        # Same release segment, only local/post label differs (e.g. 2.8.0 -> 2.8.0+cpu).
        return "other"
    return "patch"


def _version_sort_key(raw: str) -> tuple:
    try:
        return (0, Version(raw), raw)
    except InvalidVersion:
        return (1, Version("0"), raw)


def diff_package(before: PackageSnapshot | None, after: NormalizedPackage) -> list[EventDraft]:
    """Compare stored state with freshly fetched metadata and describe what changed."""
    if before is None:
        return [EventDraft(kind=PackageEventKind.tracking_started, details={"latest_version": after.latest_version})]

    events: list[EventDraft] = []
    if before.unavailable:
        events.append(EventDraft(kind=PackageEventKind.project_restored))

    incoming = {version.version: version for version in after.versions}

    added = [
        version
        for name, version in incoming.items()
        if name not in before.version_statuses and version.status != PackageVersionStatus.removed.value
    ]
    # previous_version follows every new release; the bump is measured against the last stable one,
    # so 3.1.1 -> 3.2.0rc1 -> 3.2.0 reports "pre" and then "minor".
    previous = before.latest_version
    previous_stable = before.latest_version
    for version in sorted(added, key=lambda item: _version_sort_key(item.version)):
        prerelease = _is_prerelease(version.version)
        events.append(
            EventDraft(
                kind=PackageEventKind.release_added,
                version=version.version,
                occurred_at=version.release_date,
                details={
                    "previous_version": previous,
                    "bump": classify_bump(previous_stable, version.version),
                    "prerelease": prerelease,
                },
            )
        )
        if version.status == PackageVersionStatus.yanked.value:
            events.append(_yanked_event(version.version, version.yanked_reason))
        previous = version.version
        if not prerelease:
            previous_stable = version.version

    for name in sorted(before.version_statuses, key=_version_sort_key):
        old_status = before.version_statuses[name]
        new_version = incoming.get(name)
        new_status = PackageVersionStatus(new_version.status) if new_version else PackageVersionStatus.removed
        if old_status == new_status:
            continue

        if new_status == PackageVersionStatus.removed:
            events.append(EventDraft(kind=PackageEventKind.release_removed, version=name))
        elif old_status == PackageVersionStatus.removed:
            events.append(EventDraft(kind=PackageEventKind.release_restored, version=name))
            if new_status == PackageVersionStatus.yanked:
                events.append(_yanked_event(name, new_version.yanked_reason))
        elif new_status == PackageVersionStatus.yanked:
            events.append(_yanked_event(name, new_version.yanked_reason))
        elif old_status == PackageVersionStatus.yanked:
            events.append(EventDraft(kind=PackageEventKind.release_unyanked, version=name))

    for vuln in after.vulnerabilities:
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


def _is_prerelease(raw: str) -> bool:
    try:
        parsed = Version(raw)
    except InvalidVersion:
        return False
    return parsed.is_prerelease or parsed.is_devrelease
