from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.ecosystems.pypi.versions import PyPIVersionScheme
from app.ecosystems.registry import UnknownEcosystem, get_plugin
from app.models.dependency import Dependency
from app.models.enums import DependencyType, Ecosystem, PackageEventKind
from app.models.package import Package
from app.models.package_event import PackageEvent
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.run import Run
from app.models.version import PackageVersion
from app.schemas.updates import (
    EcosystemRunInfo,
    UpdateEventItem,
    UpdateEventListResponse,
    UpdateRunInfo,
    UpdatesSummaryPackage,
    UpdatesSummaryResponse,
    UpdatesSummaryTotals,
)
from app.services.cursor import CursorError, decode_cursor, encode_cursor, parse_iso_datetime
from app.services.purls import package_purl
from app.tasks.fetch_packages import ref_from_target

router = APIRouter()

# Bump severity: higher index wins.
BUMP_ORDER = ["other", "pre", "patch", "minor", "major"]
# Flag severity: lower index is more severe (also the order flags are listed in).
FLAG_ORDER = ["unavailable", "vulnerability_added", "removed", "yanked", "deprecated", "major"]
DAILY_UPDATE = "daily_update"
_DEFAULT_SCHEME = PyPIVersionScheme()


class SummaryPeriod(str, Enum):
    day = "day"
    week = "week"


# --------------------------------------------------------------------------- pure logic


@dataclass
class SummaryEvent:
    """Event-like record consumed by `aggregate_summary` (DB-free)."""

    package: str
    kind: Any
    detected_at: datetime
    version: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    id: int = 0
    ecosystem: str = "pypi"
    namespace: str = ""
    description: str | None = None
    occurred_at: datetime | None = None
    package_id: Any = None
    origin: str = ""


def _kind_value(kind: Any) -> str:
    return str(getattr(kind, "value", kind))


def resolve_timezone(name: str) -> tzinfo:
    """Resolve an IANA name; raises ValueError for invalid names."""
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, OSError
        raise ValueError(f"Unknown timezone '{name}'") from exc


def _start_of_day(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def compute_summary_window(
    period: str,
    tz_name: str = "UTC",
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return (since, until) in UTC.

    If both `since` and `until` are given they win over `period`. A single explicit bound
    is combined with the period default for the other side. `tz_name` is validated always but
    only affects the period-derived boundary. Raises ValueError for invalid tz/period/empty window.
    """
    tz = resolve_timezone(tz_name)
    period_value = _kind_value(period)
    if period_value not in {"day", "week"}:
        raise ValueError(f"Unknown period '{period_value}'")

    now_utc = _ensure_aware(now) if now is not None else datetime.now(UTC)

    if since is not None and until is not None:
        since_utc, until_utc = _ensure_aware(since), _ensure_aware(until)
    else:
        today = now_utc.astimezone(tz).date()
        days_back = 6 if period_value == "week" else 0
        since_utc = _ensure_aware(since) if since is not None else _start_of_day(today - timedelta(days=days_back), tz)
        until_utc = _ensure_aware(until) if until is not None else now_utc

    if since_utc >= until_utc:
        raise ValueError("'since' must be earlier than 'until'")
    return since_utc, until_utc


def _scheme_for(ecosystem: str) -> Any:
    try:
        return get_plugin(ecosystem).versions
    except UnknownEcosystem:  # an ecosystem without a plugin: order as PyPI does
        return _DEFAULT_SCHEME


def _is_prerelease(scheme: Any, event: Any) -> bool:
    flag = (event.details or {}).get("prerelease")
    return bool(flag) if flag is not None else scheme.is_prerelease(event.version)


def _release_range(ecosystem: str, releases: list[Any]) -> tuple[str | None, str | None]:
    """`to` is the highest stable release of the window (the highest one if all are prereleases); `from` is where its
    chain of `previous_version` leaves the window. Backports in the window sit on their own chains and do not count."""
    scheme = _scheme_for(ecosystem)
    by_version = {r.version: r for r in releases if r.version is not None}
    if not by_version:
        return (releases[0].details or {}).get("previous_version"), None

    stable = [r for r in by_version.values() if not _is_prerelease(scheme, r)]
    to_event = max(stable or by_version.values(), key=lambda r: scheme.sort_key(r.version))
    current = to_event
    seen = {current.version}
    while True:
        previous = (current.details or {}).get("previous_version")
        # Stored events may carry the old rule's values: a previous that was already visited would loop.
        if previous in by_version and previous not in seen:
            seen.add(previous)
            current = by_version[previous]
            continue
        return previous, to_event.version


def _is_backfill(event: Any) -> bool:
    """A version that arrived with a new source: in the journal, but not a release of the digest."""
    return _kind_value(event.kind) == "release_added" and bool((event.details or {}).get("backfill"))


@dataclass
class _Kinds:
    """Versions and ids of a package's events, by what they say."""

    releases: list[Any] = field(default_factory=list)
    yanked: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)
    sources_added: list[str] = field(default_factory=list)
    vulnerabilities: list[str] = field(default_factory=list)
    has_unavailable: bool = False


def _split_by_kind(items: list[Any]) -> _Kinds:
    kinds = _Kinds()
    versioned = {
        "release_yanked": kinds.yanked,
        "release_removed": kinds.removed,
        "release_deprecated": kinds.deprecated,
    }
    for e in items:
        kind = _kind_value(e.kind)
        if kind == "release_added":
            kinds.releases.append(e)
        elif kind in versioned:
            if e.version is not None:
                versioned[kind].append(e.version)
        elif kind == "source_added":
            # The pipeline always names the registry; a version stands in only for an event without one.
            source = (e.details or {}).get("registry") or e.version
            if source is not None and str(source) not in kinds.sources_added:
                kinds.sources_added.append(str(source))
        elif kind == "vulnerability_added":
            vuln_id = (e.details or {}).get("id")
            if vuln_id:
                kinds.vulnerabilities.append(str(vuln_id))
        elif kind == "project_unavailable":
            kinds.has_unavailable = True
    return kinds


def _max_bump(releases: list[Any]) -> str | None:
    ranks = [BUMP_ORDER.index(b) for b in ((r.details or {}).get("bump") for r in releases) if b in BUMP_ORDER]
    return BUMP_ORDER[max(ranks)] if ranks else None


def _risk_flags(items: list[Any], kinds: _Kinds, max_bump: str | None) -> list[str]:
    flags: list[str] = []
    if kinds.has_unavailable:
        flags.append("unavailable")
    if any(_kind_value(e.kind) == "vulnerability_added" for e in items):
        flags.append("vulnerability_added")
    if kinds.removed:
        flags.append("removed")
    if kinds.yanked:
        flags.append("yanked")
    if kinds.deprecated:
        flags.append("deprecated")
    if max_bump == "major":
        flags.append("major")
    return flags


def _release_info(ecosystem: str, releases: list[Any]) -> tuple[str | None, str | None, datetime | None, bool]:
    """(from, to, when `to` was released, whether the window has only prereleases)."""
    if not releases:
        return None, None, None, False
    from_version, to_version = _release_range(ecosystem, releases)
    released_at = next((r.occurred_at for r in releases if r.version == to_version), None) if to_version else None
    scheme = _scheme_for(ecosystem)
    return from_version, to_version, released_at, all(_is_prerelease(scheme, r) for r in releases)


def aggregate_summary(
    events: Iterable[Any], used_by: Mapping[Any, int] | None = None
) -> list[UpdatesSummaryPackage]:
    """Aggregate events (any objects with package/version/kind/detected_at/details[/id/ecosystem/namespace
    /description/occurred_at]) per package; `pkg:pypi/x` and `pkg:npm/x` are two packages.

    `tracking_started` and backfill events are ignored; packages with no other events are not listed.
    `used_by` maps an event's `package_id` to the number of catalog packages depending on it; it is known only after a
    query over the digest's ids, so the one sort of the result happens here.
    """
    grouped: dict[tuple[str, str, str], list[Any]] = {}
    for event in events:
        if _kind_value(event.kind) == PackageEventKind.tracking_started.value or _is_backfill(event):
            continue
        key = (getattr(event, "ecosystem", "pypi"), getattr(event, "namespace", ""), event.package)
        grouped.setdefault(key, []).append(event)

    result: list[UpdatesSummaryPackage] = []
    for (ecosystem, namespace, name), items in grouped.items():
        items.sort(key=lambda e: (e.detected_at, getattr(e, "id", 0) or 0))
        kinds = _split_by_kind(items)
        from_version, to_version, released_at, prerelease_only = _release_info(ecosystem, kinds.releases)
        max_bump = _max_bump(kinds.releases)
        package_id = next((i for e in items if (i := getattr(e, "package_id", None)) is not None), None)
        result.append(
            UpdatesSummaryPackage(
                ecosystem=ecosystem,
                name=name,
                purl=package_purl(ecosystem, namespace, name),
                description=next((d for e in items if (d := getattr(e, "description", None))), None),
                from_version=from_version,
                to_version=to_version,
                released_at=released_at,
                prerelease_only=prerelease_only,
                max_bump=max_bump,
                releases=[r.version for r in kinds.releases if r.version is not None],
                yanked=kinds.yanked,
                removed=kinds.removed,
                deprecated=kinds.deprecated,
                sources_added=kinds.sources_added,
                vulnerabilities=kinds.vulnerabilities,
                flags=_risk_flags(items, kinds, max_bump),
                last_detected_at=max(e.detected_at for e in items),
                used_by=(used_by or {}).get(package_id, 0),
                origin=next((o for e in items if (o := getattr(e, "origin", ""))), ""),
            )
        )

    def sort_key(pkg: UpdatesSummaryPackage) -> tuple[int, int, float, str, str, str]:
        rank = min((FLAG_ORDER.index(f) for f in pkg.flags), default=len(FLAG_ORDER))
        return (rank, -pkg.used_by, -pkg.last_detected_at.timestamp(), pkg.name, pkg.ecosystem, pkg.purl)

    result.sort(key=sort_key)
    return result


def compute_totals(events: Iterable[Any], packages: list[UpdatesSummaryPackage]) -> UpdatesSummaryTotals:
    # Packages that are only prereleases and have no flag: the rows the page hides by default.
    totals = UpdatesSummaryTotals(
        packages=len(packages),
        prereleases=sum(1 for p in packages if p.prerelease_only and not p.flags),
        attention=sum(1 for p in packages if p.flags),
    )
    for e in events:
        kind = _kind_value(e.kind)
        if kind == "release_added":
            if not _is_backfill(e):
                totals.releases += 1
        elif kind == "release_yanked":
            totals.yanked += 1
        elif kind == "release_removed":
            totals.removed += 1
        elif kind == "release_deprecated":
            totals.deprecated += 1
        elif kind == "source_added":
            totals.sources_added += 1
        elif kind == "vulnerability_added":
            totals.vulnerabilities += 1
        elif kind == "project_unavailable":
            totals.unavailable += 1
    return totals


# --------------------------------------------------------------------------- helpers


def _run_fields(run: Run) -> dict[str, Any]:
    return {
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "packages_checked": run.packages_checked,
        "packages_changed": run.packages_changed,
        "packages_updated": run.packages_updated,
        "packages_failed": run.packages_failed,
        "packages_unavailable": run.packages_unavailable,
        "events_created": run.events_created,
    }


def serialize_run(run: Run | None) -> UpdateRunInfo | None:
    return None if run is None else UpdateRunInfo(**_run_fields(run))


async def fetch_last_update_run(db: AsyncSession) -> UpdateRunInfo | None:
    """The latest PyPI daily update, from the runs journal."""
    run = await db.scalar(
        select(Run)
        .where(Run.operation == DAILY_UPDATE, Run.ecosystem == Ecosystem.pypi)
        .order_by(Run.started_at.desc())
        .limit(1)
    )
    return serialize_run(run)


async def fetch_last_update_runs(db: AsyncSession) -> list[EcosystemRunInfo]:
    """The latest daily update of each ecosystem, ordered by ecosystem."""
    newest = (
        select(Run.id, func.row_number().over(partition_by=Run.ecosystem, order_by=Run.started_at.desc()).label("rank"))
        .where(Run.operation == DAILY_UPDATE, Run.ecosystem.is_not(None))
        .subquery()
    )
    runs = (await db.scalars(select(Run).join(newest, newest.c.id == Run.id).where(newest.c.rank == 1))).all()
    # The ecosystem filter above leaves no null ecosystems; the enum sorts by declaration, the API by name.
    return [
        EcosystemRunInfo(**_run_fields(run), ecosystem=run.ecosystem.value if run.ecosystem else "")
        for run in sorted(runs, key=lambda run: run.ecosystem.value if run.ecosystem else "")
    ]


# Dependency types that make a package depend on another; dev, build and optional ones only inflate the number.
_USED_BY_TYPES = (DependencyType.runtime, DependencyType.peer, DependencyType.provided)


async def fetch_used_by(db: AsyncSession, package_ids: Iterable[Any]) -> dict[Any, int]:
    """Per package id: how many other packages have it as a runtime/peer/provided dependency in their latest version."""
    ids = set(package_ids)
    if not ids:
        return {}
    latest = (
        select(PackageVersion.id, PackageVersion.package_id)
        .join(Package, and_(Package.id == PackageVersion.package_id, Package.latest_version == PackageVersion.version))
        .subquery()
    )
    stmt = (
        select(Dependency.resolved_package_id, func.count(func.distinct(latest.c.package_id)))
        .join(latest, latest.c.id == Dependency.version_id)
        .where(
            Dependency.resolved_package_id.in_(ids),
            Dependency.resolved_package_id != latest.c.package_id,
            Dependency.dependency_type.in_(_USED_BY_TYPES),
        )
        .group_by(Dependency.resolved_package_id)
    )
    return dict((await db.execute(stmt)).all())


def _scope(stmt, ecosystems: list[Ecosystem] | None, registry: str | None):
    """Events of packages in the given ecosystems / linked to the registry (events have no registry column)."""
    if ecosystems:
        stmt = stmt.where(Package.ecosystem.in_(ecosystems))
    if registry:
        stmt = stmt.where(
            exists(
                select(PackageRegistry.package_id)
                .join(Registry, Registry.id == PackageRegistry.registry_id)
                .where(PackageRegistry.package_id == Package.id, Registry.slug == registry)
            )
        )
    return stmt


def _decode_cursor_or_422(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        payload = decode_cursor(cursor) or {}   # None only for an empty cursor, handled above
        detected_at = parse_iso_datetime(payload["detected_at"])
        event_id = int(payload["id"])
    except CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor payload") from exc
    return _ensure_aware(detected_at), event_id


# --------------------------------------------------------------------------- endpoints


@router.get("/updates", response_model=UpdateEventListResponse)
async def list_updates(
    db: AsyncSession = Depends(get_db_session),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    kind: list[PackageEventKind] | None = Query(default=None),
    ecosystem: list[Ecosystem] | None = Query(default=None),
    registry: str | None = Query(default=None),
    package: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> UpdateEventListResponse:
    until_utc = _ensure_aware(until) if until is not None else datetime.now(UTC)
    since_utc = _ensure_aware(since) if since is not None else until_utc - timedelta(days=1)
    if since_utc >= until_utc:
        raise HTTPException(status_code=422, detail="'since' must be earlier than 'until'")
    cursor_payload = _decode_cursor_or_422(cursor)

    stmt = _scope(
        select(PackageEvent, Package.ecosystem, Package.namespace, Package.normalized_name)
        .join(Package, Package.id == PackageEvent.package_id)
        .where(PackageEvent.detected_at >= since_utc, PackageEvent.detected_at < until_utc),
        ecosystem,
        registry,
    )
    if kind:
        stmt = stmt.where(PackageEvent.kind.in_(kind))
    if package:
        # A purl names one package; a bare name is a PyPI one.
        try:
            ref = ref_from_target(package)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid package {package!r}: {exc}") from exc
        stmt = stmt.where(
            Package.ecosystem == Ecosystem(ref.ecosystem),
            func.coalesce(Package.namespace, "") == ref.namespace,
            Package.normalized_name == ref.name,
        )
    if cursor_payload:
        c_detected, c_id = cursor_payload
        stmt = stmt.where(
            or_(
                PackageEvent.detected_at < c_detected,
                and_(PackageEvent.detected_at == c_detected, PackageEvent.id < c_id),
            )
        )
    stmt = stmt.order_by(PackageEvent.detected_at.desc(), PackageEvent.id.desc()).limit(limit + 1)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = None
    if has_more and page:
        last = page[-1][0]
        next_cursor = encode_cursor({"detected_at": last.detected_at.isoformat(), "id": last.id})

    data = [
        UpdateEventItem(
            id=event.id,
            ecosystem=package_ecosystem.value,
            package=name,
            purl=package_purl(package_ecosystem.value, namespace, name),
            version=event.version,
            kind=event.kind.value,
            detected_at=event.detected_at,
            occurred_at=event.occurred_at,
            details=event.details or {},
        )
        for event, package_ecosystem, namespace, name in page
    ]
    return UpdateEventListResponse(data=data, cursor=next_cursor, has_more=has_more)


@router.get("/updates/summary", response_model=UpdatesSummaryResponse)
async def updates_summary(
    db: AsyncSession = Depends(get_db_session),
    period: SummaryPeriod = Query(default=SummaryPeriod.day),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    tz: str = Query(default="UTC"),
    ecosystem: list[Ecosystem] | None = Query(default=None),
    registry: str | None = Query(default=None),
    kind: list[PackageEventKind] | None = Query(default=None),
) -> UpdatesSummaryResponse:
    try:
        since_utc, until_utc = compute_summary_window(period.value, tz, since, until)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stmt = _scope(
        select(PackageEvent, Package.ecosystem, Package.namespace, Package.normalized_name, Package.description,
               Package.id, Package.origin)
        .join(Package, Package.id == PackageEvent.package_id)
        .where(
            PackageEvent.detected_at >= since_utc,
            PackageEvent.detected_at < until_utc,
            PackageEvent.kind != PackageEventKind.tracking_started,
        ),
        ecosystem,
        registry,
    )
    if kind:
        stmt = stmt.where(PackageEvent.kind.in_(kind))
    stmt = stmt.order_by(PackageEvent.detected_at.asc(), PackageEvent.id.asc())
    rows = (await db.execute(stmt)).all()
    events = [
        SummaryEvent(
            package=name,
            kind=event.kind,
            detected_at=event.detected_at,
            version=event.version,
            details=event.details or {},
            id=event.id,
            ecosystem=package_ecosystem.value,
            namespace=namespace or "",
            description=description,
            occurred_at=event.occurred_at,
            package_id=package_id,
            origin=origin.value,
        )
        for event, package_ecosystem, namespace, name, description, package_id, origin in rows
    ]
    used_by = await fetch_used_by(db, {e.package_id for e in events})
    packages = aggregate_summary(events, used_by)
    return UpdatesSummaryResponse(
        since=since_utc,
        until=until_utc,
        last_run=await fetch_last_update_run(db),
        last_runs=await fetch_last_update_runs(db),
        totals=compute_totals(events, packages),
        packages=packages,
    )
