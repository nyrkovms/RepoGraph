from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dependency import Dependency
from app.models.enums import DependencyType
from app.models.package import Package
from app.models.version import PackageVersion

# Dependency types that make a package depend on another; dev, build and optional ones only inflate the number.
_USED_BY_TYPES = (DependencyType.runtime, DependencyType.peer, DependencyType.provided)


def _latest_versions():
    """Each package's latest version: (id, package_id)."""
    return (
        select(PackageVersion.id, PackageVersion.package_id)
        .join(Package, and_(Package.id == PackageVersion.package_id, Package.latest_version == PackageVersion.version))
        .subquery()
    )


async def fetch_used_by(db: AsyncSession, package_ids: Iterable[Any]) -> dict[Any, int]:
    """Per package id: how many other packages have it as a runtime/peer/provided dependency in their latest version."""
    ids = set(package_ids)
    if not ids:
        return {}
    latest = _latest_versions()
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


# --- package overview: the same graph, walked past the first step -----------------------------------------------------

DependencyKey = tuple[str, str, str]   # ecosystem, namespace, name: what a dependency names, resolved or not
_OPTIONAL_TYPES = frozenset({DependencyType.optional})
_DEV_TYPES = frozenset({DependencyType.dev, DependencyType.build})


@dataclass(frozen=True, slots=True)
class DependencySummary:
    """A version's dependencies by distinct name; a name in several types or variants counts once, in the first of
    direct, optional, dev."""

    direct: int
    optional: int
    dev: int
    direct_ids: frozenset[uuid.UUID]                 # catalog packages the direct dependencies resolve to
    not_in_catalog: frozenset[DependencyKey]         # direct and optional names with no catalog package


def summarize_dependencies(rows: Iterable[tuple[DependencyKey, DependencyType, uuid.UUID | None]]) -> DependencySummary:
    types: dict[DependencyKey, set[DependencyType]] = {}
    resolved: dict[DependencyKey, set[uuid.UUID]] = {}
    for key, dep_type, package_id in rows:
        types.setdefault(key, set()).add(dep_type)
        ids = resolved.setdefault(key, set())
        if package_id is not None:
            ids.add(package_id)
    direct: set[DependencyKey] = {key for key, found in types.items() if found & set(_USED_BY_TYPES)}
    optional = {key for key, found in types.items() if found & _OPTIONAL_TYPES} - direct
    dev = {key for key, found in types.items() if found & _DEV_TYPES} - direct - optional
    return DependencySummary(
        direct=len(direct),
        optional=len(optional),
        dev=len(dev),
        direct_ids=frozenset(package_id for key in direct for package_id in resolved[key]),
        not_in_catalog=frozenset(key for key in direct | optional if not resolved[key]),
    )


def _edges():
    """Catalog edges `src depends on dst`: from each package's latest version, `_USED_BY_TYPES`, no self-edges."""
    latest = _latest_versions()
    return (
        select(latest.c.package_id.label("src"), Dependency.resolved_package_id.label("dst"))
        .join(latest, latest.c.id == Dependency.version_id)
        .where(
            Dependency.resolved_package_id.is_not(None),
            Dependency.resolved_package_id != latest.c.package_id,
            Dependency.dependency_type.in_(_USED_BY_TYPES),
        )
        .distinct()
        .subquery()
    )


async def reachable(db: AsyncSession, start_ids: Iterable[uuid.UUID], *, forward: bool) -> set[uuid.UUID]:
    """The packages reached from `start_ids` (themselves included) along the edges, or against them when `forward` is
    false. UNION, not UNION ALL, so a cycle ends the walk."""
    ids = set(start_ids)
    if not ids:
        return set()
    edges = _edges()
    seed: Select[Any] = select(Package.id.label("id")).where(Package.id.in_(ids))
    walk = seed.cte("walk", recursive=True)
    here, there = (edges.c.src, edges.c.dst) if forward else (edges.c.dst, edges.c.src)
    walk = walk.union(select(there).join(walk, walk.c.id == here))
    return set((await db.scalars(select(walk.c.id))).all())


async def license_counts(db: AsyncSession, package_ids: Iterable[uuid.UUID]) -> list[tuple[str, int]]:
    """(license, packages) for the given packages: SPDX id, else the raw string, else `unknown`; most common first."""
    ids = set(package_ids)
    if not ids:
        return []
    rows = (await db.execute(select(Package.license_spdx, Package.license_raw).where(Package.id.in_(ids)))).all()
    counts = Counter((spdx or "").strip() or (raw or "").strip() or "unknown" for spdx, raw in rows)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


async def unresolved_edges(db: AsyncSession, package_ids: Iterable[uuid.UUID]) -> set[DependencyKey]:
    """The names the given packages' latest versions depend on (`_USED_BY_TYPES`) that no catalog package answers to."""
    ids = set(package_ids)
    if not ids:
        return set()
    latest = _latest_versions()
    rows = await db.execute(
        select(Dependency.dependency_ecosystem, Dependency.dependency_namespace, Dependency.dependency_name)
        .join(latest, latest.c.id == Dependency.version_id)
        .where(
            latest.c.package_id.in_(ids),
            Dependency.resolved_package_id.is_(None),
            Dependency.dependency_type.in_(_USED_BY_TYPES),
        )
        .distinct()
    )
    return {(ecosystem.value, namespace or "", name) for ecosystem, namespace, name in rows}
