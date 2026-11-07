from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dependency import Dependency
from app.models.enums import DependencyType
from app.models.package import Package
from app.models.version import PackageVersion

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
