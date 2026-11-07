from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.packages import build_list_items, deprecated_clause
from app.db import get_db_session
from app.models.enums import Ecosystem
from app.models.package import Package
from app.schemas.packages import (
    CatalogSummaryResponse,
    EcosystemCount,
    LicenseCount,
    StaleInUse,
)
from app.services.usage import STALE_MIN_USED_BY, STALE_YEARS, latest_released_before, stale_cutoff, used_by_counts

router = APIRouter()

MOST_USED_LIMIT = 5
DEPRECATED_IN_USE_LIMIT = 5
LICENSES_LIMIT = 12


@router.get("/catalog/summary", response_model=CatalogSummaryResponse)
async def catalog_summary(
    ecosystem: Ecosystem | None = Query(default=None), db: AsyncSession = Depends(get_db_session)
) -> CatalogSummaryResponse:
    """What the home page shows: the catalog's size by ecosystem and license, and two short lists.

    `ecosystem` narrows everything but the ecosystem counts: the chips show every ecosystem whichever is chosen."""
    scope = [Package.ecosystem == ecosystem] if ecosystem else []
    ecosystem_rows = (
        await db.execute(
            select(Package.ecosystem, func.count()).group_by(Package.ecosystem).order_by(func.count().desc(), Package.ecosystem)
        )
    ).all()
    license_rows = (
        await db.execute(
            select(Package.license_spdx, func.count())
            .where(Package.license_spdx.is_not(None), Package.license_spdx != "", *scope)
            .group_by(Package.license_spdx)
            .order_by(func.count().desc(), Package.license_spdx)
            .limit(LICENSES_LIMIT)
        )
    ).all()

    counts = used_by_counts()
    by_use = (counts.c.used_by.desc(), Package.normalized_name.asc(), Package.id.asc())
    in_use = select(Package).join(counts, counts.c.package_id == Package.id).where(*scope)
    most_used = (await db.scalars(in_use.order_by(*by_use).limit(MOST_USED_LIMIT))).all()
    deprecated = (
        await db.scalars(in_use.where(deprecated_clause()).order_by(*by_use).limit(DEPRECATED_IN_USE_LIMIT))
    ).all()

    deprecated_total = await db.scalar(
        select(func.count())
        .select_from(Package)
        .join(counts, counts.c.package_id == Package.id)
        .where(deprecated_clause(), *scope)
    )

    cutoff = stale_cutoff()
    stale = await db.scalar(
        select(func.count())
        .select_from(Package)
        .join(counts, counts.c.package_id == Package.id)
        .where(counts.c.used_by >= STALE_MIN_USED_BY, latest_released_before(cutoff), *scope)
    )

    return CatalogSummaryResponse(
        ecosystems=[EcosystemCount(ecosystem=ecosystem.value, packages=total) for ecosystem, total in ecosystem_rows],
        licenses=[LicenseCount(license=license_id, count=total) for license_id, total in license_rows],
        most_used=await build_list_items(db, most_used),
        deprecated_in_use=await build_list_items(db, deprecated),
        deprecated_in_use_total=deprecated_total or 0,
        stale_in_use=StaleInUse(
            packages=stale or 0, years=STALE_YEARS, min_used_by=STALE_MIN_USED_BY, released_before=cutoff
        ),
    )
