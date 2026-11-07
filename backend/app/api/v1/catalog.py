from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.packages import build_list_items, deprecated_clause
from app.db import get_db_session
from app.models.enums import PackageVersionStatus
from app.models.package import Package
from app.models.version import PackageVersion
from app.schemas.packages import (
    CatalogSummaryResponse,
    EcosystemCount,
    LatestReleaseItem,
    LicenseCount,
    StaleInUse,
)
from app.services.usage import STALE_MIN_USED_BY, STALE_YEARS, latest_released_before, stale_cutoff, used_by_counts

router = APIRouter()

MOST_USED_LIMIT = 5
LATEST_RELEASES_LIMIT = 5
DEPRECATED_IN_USE_LIMIT = 5
LICENSES_LIMIT = 12


@router.get("/catalog/summary", response_model=CatalogSummaryResponse)
async def catalog_summary(db: AsyncSession = Depends(get_db_session)) -> CatalogSummaryResponse:
    """What the home page shows: the catalog's size by ecosystem and license, and three short lists."""
    ecosystem_rows = (
        await db.execute(
            select(Package.ecosystem, func.count()).group_by(Package.ecosystem).order_by(func.count().desc(), Package.ecosystem)
        )
    ).all()
    license_rows = (
        await db.execute(
            select(Package.license_spdx, func.count())
            .where(Package.license_spdx.is_not(None), Package.license_spdx != "")
            .group_by(Package.license_spdx)
            .order_by(func.count().desc(), Package.license_spdx)
            .limit(LICENSES_LIMIT)
        )
    ).all()

    counts = used_by_counts()
    by_use = (counts.c.used_by.desc(), Package.normalized_name.asc(), Package.id.asc())
    in_use = select(Package).join(counts, counts.c.package_id == Package.id)
    most_used = (await db.scalars(in_use.order_by(*by_use).limit(MOST_USED_LIMIT))).all()
    deprecated = (
        await db.scalars(in_use.where(deprecated_clause()).order_by(*by_use).limit(DEPRECATED_IN_USE_LIMIT))
    ).all()

    deprecated_total = await db.scalar(
        select(func.count()).select_from(Package).join(counts, counts.c.package_id == Package.id).where(deprecated_clause())
    )

    latest_rows = (
        await db.execute(
            select(Package, PackageVersion.release_date)
            .join(
                PackageVersion,
                (PackageVersion.package_id == Package.id) & (PackageVersion.version == Package.latest_version),
            )
            .where(
                PackageVersion.status == PackageVersionStatus.active,
                PackageVersion.is_prerelease.is_(False),
                PackageVersion.release_date.is_not(None),
            )
            .order_by(PackageVersion.release_date.desc(), Package.id)
            .limit(LATEST_RELEASES_LIMIT)
        )
    ).all()

    cutoff = stale_cutoff()
    stale = await db.scalar(
        select(func.count())
        .select_from(Package)
        .join(counts, counts.c.package_id == Package.id)
        .where(counts.c.used_by >= STALE_MIN_USED_BY, latest_released_before(cutoff))
    )

    latest_items = await build_list_items(db, [package for package, _released in latest_rows])
    return CatalogSummaryResponse(
        ecosystems=[EcosystemCount(ecosystem=ecosystem.value, packages=total) for ecosystem, total in ecosystem_rows],
        licenses=[LicenseCount(license=license_id, count=total) for license_id, total in license_rows],
        most_used=await build_list_items(db, most_used),
        latest_releases=[
            LatestReleaseItem(**item.model_dump(), released_at=released)
            for item, (_package, released) in zip(latest_items, latest_rows, strict=True)
        ],
        deprecated_in_use=await build_list_items(db, deprecated),
        deprecated_in_use_total=deprecated_total or 0,
        stale_in_use=StaleInUse(
            packages=stale or 0, years=STALE_YEARS, min_used_by=STALE_MIN_USED_BY, released_before=cutoff
        ),
    )
