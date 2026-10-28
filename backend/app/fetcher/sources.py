"""Registries and source profiles from the database as contract types (SPEC-MULTI-ECOSYSTEM.md §6)."""

import logging
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ecosystems.base import Ecosystem, Protocol_, RegistryConfig, SourceProfile
from app.models.enums import Ecosystem as EcosystemEnum
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.source_profile import SourceProfile as SourceProfileRow
from app.models.source_profile import SourceProfileRegistry

logger = logging.getLogger(__name__)

# Global priority: a lower number wins; the slug breaks ties so the order is stable.
_PRIORITY_ORDER = (Registry.priority, Registry.slug)


def registry_config(row: Registry, routing_override: dict | None = None) -> RegistryConfig:
    if row.protocol is None:
        raise ValueError(f"registry {row.slug} has no protocol")
    return RegistryConfig(
        id=str(row.id),
        slug=row.slug,
        ecosystem=cast(Ecosystem, row.ecosystem.value),
        protocol=cast(Protocol_, row.protocol.value),
        base_url=row.base_url,
        priority=row.priority,
        config=row.config or {},
        routing=routing_override if routing_override is not None else row.routing,
        auth_type=row.auth_type,  # checked by ck_registries_auth_type
        credentials_ref=row.credentials_ref,
    )


def _configs(rows: Sequence[Registry]) -> list[RegistryConfig]:
    configs = []
    for row in rows:
        try:
            configs.append(registry_config(row))
        except ValueError:
            logger.warning("Registry %s skipped: no protocol", row.slug)
    return configs


async def enabled_registries(db: AsyncSession, ecosystem: str) -> list[RegistryConfig]:
    rows = (
        await db.scalars(
            select(Registry).where(Registry.ecosystem == EcosystemEnum(ecosystem), Registry.enabled.is_(True))
            .order_by(*_PRIORITY_ORDER)
        )
    ).all()
    return _configs(rows)


async def default_profile(db: AsyncSession, ecosystem: str) -> SourceProfile:
    """The ecosystem's default profile; without one, every enabled registry in priority order."""
    profile = await db.scalar(
        select(SourceProfileRow).where(SourceProfileRow.ecosystem == EcosystemEnum(ecosystem), SourceProfileRow.is_default)
    )
    if profile is None:
        return SourceProfile(ecosystem=cast(Ecosystem, ecosystem), slug="default",
                             registries=await enabled_registries(db, ecosystem))

    rows = (
        await db.execute(
            select(Registry, SourceProfileRegistry.routing_override)
            .join(SourceProfileRegistry, SourceProfileRegistry.registry_id == Registry.id)
            .where(SourceProfileRegistry.profile_id == profile.id, Registry.enabled.is_(True))
            .order_by(SourceProfileRegistry.position)
        )
    ).all()
    registries = []
    for row, override in rows:
        try:
            registries.append(registry_config(row, override))
        except ValueError:
            logger.warning("Registry %s skipped in profile %s: no protocol", row.slug, profile.slug)
    return SourceProfile(ecosystem=cast(Ecosystem, ecosystem), slug=profile.slug, registries=registries)


async def package_registries(db: AsyncSession, package_id: object) -> list[RegistryConfig]:
    """Enabled registries where the package is known (also those it went missing from), in priority order."""
    rows = (
        await db.scalars(
            select(Registry)
            .join(PackageRegistry, PackageRegistry.registry_id == Registry.id)
            .where(PackageRegistry.package_id == package_id, Registry.enabled.is_(True))
            .order_by(*_PRIORITY_ORDER)
        )
    ).all()
    return _configs(rows)


def by_priority(registries: Sequence[RegistryConfig]) -> list[RegistryConfig]:
    return sorted(registries, key=lambda r: (r.priority, r.slug))
