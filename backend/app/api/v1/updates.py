from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.models.enums import PackageEventKind
from app.models.package import Package
from app.models.package_event import PackageEvent
from app.models.update_run import UpdateRun
from app.normalizers.names import normalize_package_name
from app.schemas.updates import (
    UpdateEventItem,
    UpdateEventListResponse,
    UpdateRunInfo,
    UpdatesSummaryPackage,
    UpdatesSummaryResponse,
    UpdatesSummaryTotals,
)
from app.services.cursor import CursorError, decode_cursor, encode_cursor, parse_iso_datetime

router = APIRouter()

# Bump severity: higher index wins.
BUMP_ORDER = ["other", "pre", "patch", "minor", "major"]
# Flag severity: lower index is more severe (also the order flags are listed in).
FLAG_ORDER = ["unavailable", "vulnerability_added", "removed", "yanked", "major"]


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


def _kind_value(kind: Any) -> str:
    return str(getattr(kind, "value", kind))


def resolve_timezone(name: str) -> tzinfo:
    """Resolve an IANA name; raises ValueError for invalid names."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, OSError
        raise ValueError(f"Unknown timezone '{name}'") from exc


def _start_of_day(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    now_utc = _ensure_aware(now) if now is not None else datetime.now(timezone.utc)

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


def aggregate_summary(events: Iterable[Any]) -> list[UpdatesSummaryPackage]:
    """Aggregate events (any objects with package/version/kind/detected_at/details[/id]) per package.

    `tracking_started` events are ignored; packages with no other events are not listed.
    """
    grouped: dict[str, list[Any]] = {}
    for event in events:
        if _kind_value(event.kind) == PackageEventKind.tracking_started.value:
            continue
        grouped.setdefault(event.package, []).append(event)

    result: list[UpdatesSummaryPackage] = []
    for name, items in grouped.items():
        items.sort(key=lambda e: (e.detected_at, getattr(e, "id", 0) or 0))
        releases: list[Any] = []
        yanked: list[str] = []
        removed: list[str] = []
        vulnerabilities: list[str] = []
        has_unavailable = False
        for e in items:
            kind = _kind_value(e.kind)
            if kind == "release_added":
                releases.append(e)
            elif kind == "release_yanked":
                if e.version is not None:
                    yanked.append(e.version)
            elif kind == "release_removed":
                if e.version is not None:
                    removed.append(e.version)
            elif kind == "vulnerability_added":
                vuln_id = (e.details or {}).get("id")
                if vuln_id:
                    vulnerabilities.append(str(vuln_id))
            elif kind == "project_unavailable":
                has_unavailable = True

        from_version = None
        to_version = None
        max_bump = None
        if releases:
            from_version = (releases[0].details or {}).get("previous_version")
            to_version = releases[-1].version
            ranks = [
                BUMP_ORDER.index(b)
                for b in ((r.details or {}).get("bump") for r in releases)
                if b in BUMP_ORDER
            ]
            if ranks:
                max_bump = BUMP_ORDER[max(ranks)]

        flags: list[str] = []
        if has_unavailable:
            flags.append("unavailable")
        if any(_kind_value(e.kind) == "vulnerability_added" for e in items):
            flags.append("vulnerability_added")
        if removed:
            flags.append("removed")
        if yanked:
            flags.append("yanked")
        if max_bump == "major":
            flags.append("major")

        result.append(
            UpdatesSummaryPackage(
                name=name,
                from_version=from_version,
                to_version=to_version,
                max_bump=max_bump,
                releases=[r.version for r in releases if r.version is not None],
                yanked=yanked,
                removed=removed,
                vulnerabilities=vulnerabilities,
                flags=flags,
                last_detected_at=max(e.detected_at for e in items),
            )
        )

    def sort_key(pkg: UpdatesSummaryPackage) -> tuple[int, float, str]:
        rank = min((FLAG_ORDER.index(f) for f in pkg.flags), default=len(FLAG_ORDER))
        return (rank, -pkg.last_detected_at.timestamp(), pkg.name)

    result.sort(key=sort_key)
    return result


def compute_totals(events: Iterable[Any], packages: list[UpdatesSummaryPackage]) -> UpdatesSummaryTotals:
    totals = UpdatesSummaryTotals(packages=len(packages))
    for e in events:
        kind = _kind_value(e.kind)
        if kind == "release_added":
            totals.releases += 1
        elif kind == "release_yanked":
            totals.yanked += 1
        elif kind == "release_removed":
            totals.removed += 1
        elif kind == "vulnerability_added":
            totals.vulnerabilities += 1
        elif kind == "project_unavailable":
            totals.unavailable += 1
    return totals


# --------------------------------------------------------------------------- helpers


def serialize_update_run(run: UpdateRun | None) -> UpdateRunInfo | None:
    if run is None:
        return None
    return UpdateRunInfo(
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value,
        packages_checked=run.packages_checked,
        packages_changed=run.packages_changed,
        packages_updated=run.packages_updated,
        packages_failed=run.packages_failed,
        packages_unavailable=run.packages_unavailable,
        events_created=run.events_created,
    )


async def fetch_last_update_run(db: AsyncSession) -> UpdateRunInfo | None:
    run = await db.scalar(select(UpdateRun).order_by(UpdateRun.started_at.desc()).limit(1))
    return serialize_update_run(run)


def _decode_cursor_or_422(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        payload = decode_cursor(cursor)
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
    package: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> UpdateEventListResponse:
    until_utc = _ensure_aware(until) if until is not None else datetime.now(timezone.utc)
    since_utc = _ensure_aware(since) if since is not None else until_utc - timedelta(days=1)
    if since_utc >= until_utc:
        raise HTTPException(status_code=422, detail="'since' must be earlier than 'until'")
    cursor_payload = _decode_cursor_or_422(cursor)

    stmt = (
        select(PackageEvent, Package.normalized_name)
        .join(Package, Package.id == PackageEvent.package_id)
        .where(PackageEvent.detected_at >= since_utc, PackageEvent.detected_at < until_utc)
    )
    if kind:
        stmt = stmt.where(PackageEvent.kind.in_(kind))
    if package:
        stmt = stmt.where(Package.normalized_name == normalize_package_name(package))
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
            package=name,
            version=event.version,
            kind=event.kind.value,
            detected_at=event.detected_at,
            occurred_at=event.occurred_at,
            details=event.details or {},
        )
        for event, name in page
    ]
    return UpdateEventListResponse(data=data, cursor=next_cursor, has_more=has_more)


@router.get("/updates/summary", response_model=UpdatesSummaryResponse)
async def updates_summary(
    db: AsyncSession = Depends(get_db_session),
    period: SummaryPeriod = Query(default=SummaryPeriod.day),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    tz: str = Query(default="UTC"),
) -> UpdatesSummaryResponse:
    try:
        since_utc, until_utc = compute_summary_window(period.value, tz, since, until)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stmt = (
        select(PackageEvent, Package.normalized_name)
        .join(Package, Package.id == PackageEvent.package_id)
        .where(
            PackageEvent.detected_at >= since_utc,
            PackageEvent.detected_at < until_utc,
            PackageEvent.kind != PackageEventKind.tracking_started,
        )
        .order_by(PackageEvent.detected_at.asc(), PackageEvent.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    events = [
        SummaryEvent(
            package=name,
            kind=event.kind,
            detected_at=event.detected_at,
            version=event.version,
            details=event.details or {},
            id=event.id,
        )
        for event, name in rows
    ]
    packages = aggregate_summary(events)
    return UpdatesSummaryResponse(
        since=since_utc,
        until=until_utc,
        last_run=await fetch_last_update_run(db),
        totals=compute_totals(events, packages),
        packages=packages,
    )
