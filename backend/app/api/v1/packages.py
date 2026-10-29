from __future__ import annotations

from datetime import datetime
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.models.enums import Ecosystem, PackageOrigin
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.vulnerability import PackageVulnerability
from app.schemas.packages import PackageListItem, PackageListResponse
from app.services.cursor import CursorError, decode_cursor, encode_cursor, parse_iso_datetime, parse_uuid
from app.services.purls import package_purl

router = APIRouter()


class PackageSort(str, Enum):
    name = "name"
    updated_at = "updated_at"
    downloads_total = "downloads_total"


@router.get("/packages", response_model=PackageListResponse)
async def list_packages(
    db: AsyncSession = Depends(get_db_session),
    ecosystem: list[Ecosystem] | None = Query(default=None),
    registry: str | None = Query(default=None),
    origin: list[PackageOrigin] | None = Query(default=None),
    license: str | None = Query(default=None),
    has_vulnerabilities: bool | None = Query(default=None),
    updated_after: datetime | None = Query(default=None),
    sort: PackageSort = Query(default=PackageSort.name),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> PackageListResponse:
    packages, next_cursor, has_more = await _query_packages(
        db=db,
        q=None,
        ecosystems=ecosystem,
        registry=registry,
        origins=origin,
        license=license,
        has_vulnerabilities=has_vulnerabilities,
        updated_after=updated_after,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )
    return PackageListResponse(data=packages, cursor=next_cursor, has_more=has_more)



async def _query_packages(
    db: AsyncSession,
    q: str | None,
    ecosystems: list[Ecosystem] | None,
    registry: str | None,
    origins: list[PackageOrigin] | None,
    license: str | None,
    has_vulnerabilities: bool | None,
    updated_after: datetime | None,
    sort: PackageSort,
    limit: int,
    cursor: str | None,
) -> tuple[list[PackageListItem], str | None, bool]:
    cursor_payload = _decode_cursor_or_422(cursor)

    stmt = select(Package)

    if ecosystems:
        stmt = stmt.where(Package.ecosystem.in_(ecosystems))

    if registry:
        # An unknown slug matches nothing: an empty page, not an error.
        stmt = stmt.where(
            exists(
                select(PackageRegistry.package_id)
                .join(Registry, Registry.id == PackageRegistry.registry_id)
                .where(PackageRegistry.package_id == Package.id, Registry.slug == registry)
            )
        )

    if origins:
        stmt = stmt.where(Package.origin.in_(origins))

    if q:
        needle = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Package.normalized_name).like(needle),
                func.lower(Package.name).like(needle),
                func.lower(func.coalesce(Package.description, "")).like(needle),
            )
        )

    if license:
        stmt = stmt.where(Package.license_spdx == license)

    if updated_after:
        stmt = stmt.where(Package.updated_at > updated_after)

    vuln_exists = exists(
        select(PackageVulnerability.package_id).where(PackageVulnerability.package_id == Package.id)
    )
    if has_vulnerabilities is True:
        stmt = stmt.where(vuln_exists)
    elif has_vulnerabilities is False:
        stmt = stmt.where(~vuln_exists)

    sort_key, ordered_stmt = _apply_package_sort(stmt, sort)

    if cursor_payload:
        if cursor_payload.get("sort") != sort.value:
            raise HTTPException(status_code=422, detail="cursor sort mismatch")

        last_id = parse_uuid(cursor_payload["id"])
        if sort == PackageSort.name:
            last_key = cursor_payload["key"]
            ordered_stmt = ordered_stmt.where(
                or_(
                    Package.normalized_name > last_key,
                    and_(Package.normalized_name == last_key, Package.id > last_id),
                )
            )
        elif sort == PackageSort.updated_at:
            last_key = parse_iso_datetime(cursor_payload["key"])
            ordered_stmt = ordered_stmt.where(
                or_(
                    Package.updated_at < last_key,
                    and_(Package.updated_at == last_key, Package.id > last_id),
                )
            )
        elif sort == PackageSort.downloads_total:
            last_key = int(cursor_payload["key"])
            current = func.coalesce(Package.downloads_total, -1)
            ordered_stmt = ordered_stmt.where(or_(current < last_key, and_(current == last_key, Package.id > last_id)))

    rows = (await db.scalars(ordered_stmt.limit(limit + 1))).all()
    has_more = len(rows) > limit
    data_rows = rows[:limit]

    package_ids = [row.id for row in data_rows]
    vuln_pkg_ids = set()
    if package_ids:
        vuln_rows = (
            await db.scalars(
                select(PackageVulnerability.package_id).where(PackageVulnerability.package_id.in_(package_ids)).distinct()
            )
        ).all()
        vuln_pkg_ids = set(vuln_rows)

    response_items = [
        PackageListItem(
            ecosystem=row.ecosystem.value,
            namespace=row.namespace or "",
            name=row.name,
            normalized_name=row.normalized_name,
            purl=package_purl(row.ecosystem.value, row.namespace, row.normalized_name),
            origin=row.origin.value,
            description=row.description,
            license_spdx=row.license_spdx,
            latest_version=row.latest_version,
            updated_at=row.updated_at,
            has_vulnerabilities=row.id in vuln_pkg_ids,
        )
        for row in data_rows
    ]

    next_cursor = None
    if has_more and data_rows:
        last = data_rows[-1]
        next_cursor = encode_cursor(
            {
                "sort": sort.value,
                "id": str(last.id),
                "key": _cursor_key_for_sort(last, sort, sort_key),
            }
        )

    return response_items, next_cursor, has_more


def _apply_package_sort(stmt, sort: PackageSort):
    if sort == PackageSort.name:
        return "name", stmt.order_by(Package.normalized_name.asc(), Package.id.asc())
    if sort == PackageSort.updated_at:
        return "updated_at", stmt.order_by(Package.updated_at.desc(), Package.id.asc())
    if sort == PackageSort.downloads_total:
        return "downloads_total", stmt.order_by(func.coalesce(Package.downloads_total, -1).desc(), Package.id.asc())
    return "name", stmt.order_by(Package.normalized_name.asc(), Package.id.asc())


def _cursor_key_for_sort(row: Package, sort: PackageSort, sort_key: str) -> str:
    if sort == PackageSort.name:
        return row.normalized_name
    if sort == PackageSort.updated_at:
        return row.updated_at.isoformat()
    if sort == PackageSort.downloads_total:
        return str(row.downloads_total if row.downloads_total is not None else -1)
    return getattr(row, sort_key)


def _decode_cursor_or_422(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        return decode_cursor(cursor)
    except CursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
