from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db import get_db_session
from app.ecosystems import purl as purls
from app.ecosystems.base import PackageRef
from app.ecosystems.registry import UnknownEcosystem, get_plugin
from app.models.enums import Ecosystem, PackageOrigin
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.vulnerability import PackageVulnerability
from app.schemas.packages import PackageListItem, PackageListResponse
from app.services.cursor import CursorError, decode_cursor, encode_cursor, parse_iso_datetime, parse_uuid
from app.services.purls import package_purl
from app.services.usage import fetch_used_by, latest_released_before, used_by_counts

router = APIRouter()


class PackageSort(str, Enum):
    name = "name"
    updated_at = "updated_at"
    downloads_total = "downloads_total"
    # Exact name, then names starting with the query, then the rest. The default of /search; without `q` it is `name`.
    relevance = "relevance"
    # Packages the most other catalog packages depend on, then name.
    used_by = "used_by"


def deprecated_clause():
    """SQL condition: the package is deprecated. `deprecation` is JSONB and most rows hold JSON `null`, not SQL NULL."""
    return and_(Package.deprecation.is_not(None), Package.deprecation != text("'null'::jsonb"))


@router.get("/packages", response_model=PackageListResponse)
async def list_packages(
    db: AsyncSession = Depends(get_db_session),
    ecosystem: list[Ecosystem] | None = Query(default=None),
    registry: str | None = Query(default=None),
    origin: list[PackageOrigin] | None = Query(default=None),
    license: str | None = Query(default=None),
    has_vulnerabilities: bool | None = Query(default=None),
    updated_after: datetime | None = Query(default=None),
    min_used_by: int | None = Query(default=None, ge=0),
    deprecated: bool | None = Query(default=None),
    released_before: datetime | None = Query(default=None),
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
        min_used_by=min_used_by,
        deprecated=deprecated,
        released_before=released_before,
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
    min_used_by: int | None,
    deprecated: bool | None,
    released_before: datetime | None,
    sort: PackageSort,
    limit: int,
    cursor: str | None,
) -> tuple[list[PackageListItem], str | None, bool]:
    cursor_payload = _decode_cursor_or_422(cursor)

    stmt = select(Package)

    purl_ref: PackageRef | None = None
    if q and q[:4].lower() == "pkg:":
        # A purl names one package: its version and qualifiers (except the rpm distro) do not narrow the search.
        purl_ref = _ref_from_purl(q.strip())
        if purl_ref is None:
            return [], None, False
        stmt = stmt.where(
            Package.ecosystem == Ecosystem(purl_ref.ecosystem),
            func.coalesce(Package.namespace, "") == purl_ref.namespace,
            Package.normalized_name == purl_ref.name,
        )
        q = None
    if sort == PackageSort.relevance and not q:
        sort = PackageSort.name

    used_by_expr: ColumnElement[int] | None = None
    if sort == PackageSort.used_by or min_used_by:
        counts = used_by_counts()
        stmt = stmt.outerjoin(counts, counts.c.package_id == Package.id)
        used_by_expr = func.coalesce(counts.c.used_by, 0)
        if min_used_by:
            stmt = stmt.where(used_by_expr >= min_used_by)

    if deprecated is True:
        stmt = stmt.where(deprecated_clause())
    elif deprecated is False:
        stmt = stmt.where(~deprecated_clause())

    if released_before:
        stmt = stmt.where(latest_released_before(released_before))

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

    sort_key, ordered_stmt = _apply_package_sort(stmt, sort, q, used_by_expr)

    if cursor_payload:
        ordered_stmt = _after_cursor(ordered_stmt, sort, cursor_payload, q, used_by_expr)

    rows = (await db.scalars(ordered_stmt.limit(limit + 1))).all()
    has_more = len(rows) > limit
    data_rows = rows[:limit]

    response_items = await build_list_items(db, data_rows)

    next_cursor = None
    if has_more and data_rows:
        last = data_rows[-1]
        payload: dict[str, object] = {"sort": sort.value, "id": str(last.id), "key": _cursor_key_for_sort(last, sort, sort_key)}
        if sort == PackageSort.relevance:
            payload["rank"] = _relevance_rank_of(last.normalized_name, q or "")
        if sort == PackageSort.used_by:
            payload["used_by"] = response_items[-1].used_by
        next_cursor = encode_cursor(payload)

    return response_items, next_cursor, has_more


def _after_cursor(ordered_stmt, sort: PackageSort, cursor_payload: dict, q: str | None, used_by_expr):
    """The rows after the cursor in the order `_apply_package_sort` gives."""
    if cursor_payload.get("sort") != sort.value:
        raise HTTPException(status_code=422, detail="cursor sort mismatch")

    last_id = parse_uuid(cursor_payload["id"])
    if sort == PackageSort.relevance:
        last_key = cursor_payload["key"]
        last_rank = int(cursor_payload["rank"])
        rank = _relevance_rank(q or "")
        ordered_stmt = ordered_stmt.where(
            or_(
                rank > last_rank,
                and_(
                    rank == last_rank,
                    or_(
                        Package.normalized_name > last_key,
                        and_(Package.normalized_name == last_key, Package.id > last_id),
                    ),
                ),
            )
        )
    elif sort == PackageSort.used_by:
        assert used_by_expr is not None
        last_key = cursor_payload["key"]
        last_used = int(cursor_payload["used_by"])
        ordered_stmt = ordered_stmt.where(
            or_(
                used_by_expr < last_used,
                and_(
                    used_by_expr == last_used,
                    or_(
                        Package.normalized_name > last_key,
                        and_(Package.normalized_name == last_key, Package.id > last_id),
                    ),
                ),
            )
        )
    elif sort == PackageSort.name:
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
    return ordered_stmt


async def build_list_items(db: AsyncSession, rows: Sequence[Package]) -> list[PackageListItem]:
    """List rows for the packages, with the vulnerability flag and the `used_by` count looked up for the page."""
    package_ids = [row.id for row in rows]
    vuln_pkg_ids: set = set()
    if package_ids:
        vuln_rows = (
            await db.scalars(
                select(PackageVulnerability.package_id).where(PackageVulnerability.package_id.in_(package_ids)).distinct()
            )
        ).all()
        vuln_pkg_ids = set(vuln_rows)
    used_by = await fetch_used_by(db, package_ids)

    return [
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
            used_by=used_by.get(row.id, 0),
        )
        for row in rows
    ]


def _relevance_rank(q: str):
    """SQL rank of a package for the query: 0 exact `normalized_name`, 1 starts with it, 2 the rest."""
    needle = q.lower()
    name = func.lower(Package.normalized_name)
    return case((name == needle, 0), (name.startswith(needle, autoescape=True), 1), else_=2)


def _relevance_rank_of(normalized_name: str, q: str) -> int:
    """The same rank for a loaded row, for the cursor."""
    name, needle = normalized_name.lower(), q.lower()
    return 0 if name == needle else 1 if name.startswith(needle) else 2


def _ref_from_purl(raw: str) -> PackageRef | None:
    """The package a purl names, resolved as `/purl/{purl}` does (the plugin knows the repo prefix of rpm names);
    None for a purl that does not parse or names no known ecosystem."""
    try:
        plugin = get_plugin(purls.ecosystem_for(purls.parse(raw).type))
        ref, _version = plugin.coordinates.from_purl(raw)
    except (UnknownEcosystem, ValueError):
        return None
    return ref


def _apply_package_sort(stmt, sort: PackageSort, q: str | None = None, used_by_expr=None):
    if sort == PackageSort.used_by:
        return "name", stmt.order_by(used_by_expr.desc(), Package.normalized_name.asc(), Package.id.asc())
    if sort == PackageSort.relevance:
        return "name", stmt.order_by(_relevance_rank(q or ""), Package.normalized_name.asc(), Package.id.asc())
    if sort == PackageSort.name:
        return "name", stmt.order_by(Package.normalized_name.asc(), Package.id.asc())
    if sort == PackageSort.updated_at:
        return "updated_at", stmt.order_by(Package.updated_at.desc(), Package.id.asc())
    if sort == PackageSort.downloads_total:
        return "downloads_total", stmt.order_by(func.coalesce(Package.downloads_total, -1).desc(), Package.id.asc())
    return "name", stmt.order_by(Package.normalized_name.asc(), Package.id.asc())


def _cursor_key_for_sort(row: Package, sort: PackageSort, sort_key: str) -> str:
    if sort in (PackageSort.name, PackageSort.relevance, PackageSort.used_by):
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
