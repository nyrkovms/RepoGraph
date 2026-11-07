from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, exists, func, or_, select
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


def used_by_counts():
    """Subquery (package_id, used_by): how many other packages have the package as a runtime/peer/provided dependency
    in their latest version. A package nobody uses has no row. `fetch_used_by` and the list sort/filter share it."""
    latest = _latest_versions()
    return (
        select(
            Dependency.resolved_package_id.label("package_id"),
            func.count(func.distinct(latest.c.package_id)).label("used_by"),
        )
        .join(latest, latest.c.id == Dependency.version_id)
        .where(
            Dependency.resolved_package_id.is_not(None),
            Dependency.resolved_package_id != latest.c.package_id,
            Dependency.dependency_type.in_(_USED_BY_TYPES),
        )
        .group_by(Dependency.resolved_package_id)
        .subquery()
    )


async def fetch_used_by(db: AsyncSession, package_ids: Iterable[Any]) -> dict[Any, int]:
    """Per package id: how many other packages use it (see `used_by_counts`)."""
    ids = set(package_ids)
    if not ids:
        return {}
    counts = used_by_counts()
    stmt = select(counts.c.package_id, counts.c.used_by).where(counts.c.package_id.in_(ids))
    return dict((await db.execute(stmt)).all())


def latest_released_before(moment: datetime):
    """SQL condition: the package's latest version was released before `moment`."""
    return exists(
        select(PackageVersion.id).where(
            PackageVersion.package_id == Package.id,
            PackageVersion.version == Package.latest_version,
            PackageVersion.release_date < moment,
        )
    )


# "No release in 3+ years, used by 5+": the home page's "worth a look" list (SPEC-CATALOG-PAGES section 9).
STALE_YEARS = 3
STALE_MIN_USED_BY = 5


def stale_cutoff(now: datetime | None = None) -> datetime:
    """Start of the UTC day `STALE_YEARS` ago: stable through the day, so the link built from it keeps its meaning."""
    now = now or datetime.now(UTC)
    return (now - timedelta(days=round(365.25 * STALE_YEARS))).replace(hour=0, minute=0, second=0, microsecond=0)


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


def summarize_dependencies(
    rows: Iterable[tuple[DependencyKey, DependencyType, uuid.UUID | None]], self_id: uuid.UUID | None = None
) -> DependencySummary:
    """`self_id` is the package the rows belong to: a row that resolves to it is a dependency on itself, which the graph's
    edges leave out (`_edges`), so it is left out here too."""
    types: dict[DependencyKey, set[DependencyType]] = {}
    resolved: dict[DependencyKey, set[uuid.UUID]] = {}
    for key, dep_type, package_id in rows:
        if self_id is not None and package_id == self_id:
            continue
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


# --- dependents: who breaks when the package changes ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dependent:
    package: Package
    direct: bool
    dependency_types: tuple[DependencyType, ...]   # of the direct edges; empty for an indirect dependent
    requires: tuple[str, ...]                      # distinct constraints of the direct edges; empty for an indirect one


@dataclass(frozen=True, slots=True)
class DependentsPage:
    direct_total: int
    indirect_total: int
    rows: list[Dependent]
    has_more: bool


async def direct_dependent_ids(db: AsyncSession, package_id: uuid.UUID) -> set[uuid.UUID]:
    """Packages with an edge to `package_id`: the `used_by` count is the size of this set."""
    edges = _edges()
    return set((await db.scalars(select(edges.c.src).where(edges.c.dst == package_id))).all())


async def fetch_dependents(
    db: AsyncSession,
    package_id: uuid.UUID,
    *,
    relation: str | None,
    limit: int,
    after: tuple[bool, str, uuid.UUID] | None,
) -> DependentsPage:
    """Dependents of the package over the same graph as `used_by` and the overview, direct first, then by
    `normalized_name` and id. `relation` is `direct`, `indirect` or None for both; `after` is the (indirect, name, id)
    of the last row of the previous page. The ordering and the cursor comparison are in SQL, so they follow the
    database's collation."""
    direct_ids = await direct_dependent_ids(db, package_id)
    everyone = (await reachable(db, [package_id], forward=False)) - {package_id}
    indirect_ids = everyone - direct_ids
    wanted = {"direct": direct_ids, "indirect": indirect_ids}.get(relation or "", everyone)
    page = DependentsPage(direct_total=len(direct_ids), indirect_total=len(indirect_ids), rows=[], has_more=False)
    if not wanted:
        return page

    is_indirect = case((Package.id.in_(direct_ids), 0), else_=1)
    stmt = select(Package, is_indirect.label("indirect")).where(Package.id.in_(wanted))
    if after is not None:
        was_indirect, name, last_id = after
        stmt = stmt.where(or_(
            is_indirect > int(was_indirect),
            and_(is_indirect == int(was_indirect),
                 or_(Package.normalized_name > name, and_(Package.normalized_name == name, Package.id > last_id))),
        ))
    found = (await db.execute(
        stmt.order_by(is_indirect, Package.normalized_name, Package.id).limit(limit + 1)
    )).all()
    chosen = found[:limit]

    asked: dict[uuid.UUID, set[tuple[DependencyType, str]]] = {}
    direct_on_page = [row.id for row, indirect in chosen if not indirect]
    if direct_on_page:
        latest = _latest_versions()
        edges = await db.execute(
            select(latest.c.package_id, Dependency.dependency_type, Dependency.version_constraint)
            .join(latest, latest.c.id == Dependency.version_id)
            .where(
                Dependency.resolved_package_id == package_id,
                latest.c.package_id.in_(direct_on_page),
                Dependency.dependency_type.in_(_USED_BY_TYPES),
            )
        )
        for src, dep_type, constraint in edges:
            asked.setdefault(src, set()).add((dep_type, constraint))

    rows = [
        Dependent(
            package=row,
            direct=not indirect,
            dependency_types=tuple(sorted({t for t, _ in asked.get(row.id, ())}, key=lambda t: t.value)),
            requires=tuple(sorted({c for _, c in asked.get(row.id, ()) if c})),
        )
        for row, indirect in chosen
    ]
    return DependentsPage(direct_total=page.direct_total, indirect_total=page.indirect_total, rows=rows,
                          has_more=len(found) > limit)
