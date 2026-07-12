"""Effective capabilities (classifire invariant #4).

effective = machine (current CLASSIFIER_VERSION) − user(present=false)
          + user(present=true); user wins on conflict; a version-specific
annotation beats the "all versions" one (version=''). Applied at every read
point: package card, filters, stats, exports.
"""

from sqlalchemy import String, and_, cast, exists, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier import CLASSIFIER_VERSION
from app.models.classification import (
    Classification,
    ClassificationCapability,
    ClassificationFacet,
    PackageRelation,
    UserCapability,
)
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.version import PackageVersion


def merge_effective(machine: list[str], user_rows: list[tuple[str, str, bool]]) -> list[dict]:
    """user_rows: (capability, version_scope, present); '' scope = all versions.

    Returns [{"name": ..., "origin": "structural"|"user"}] sorted by name.
    """
    decision: dict[str, tuple[int, bool]] = {}
    for capability, scope, present in user_rows:
        rank = 1 if scope == "" else 2  # конкретная версия сильнее общей пометки
        current = decision.get(capability)
        if current is None or rank >= current[0]:
            decision[capability] = (rank, present)

    effective: dict[str, str] = {}
    for capability in machine:
        verdict = decision.get(capability)
        if verdict is not None and verdict[1] is False:
            continue
        effective[capability] = "structural"
    for capability, (_rank, present) in decision.items():
        if present and capability not in effective:
            effective[capability] = "user"
    return [{"name": name, "origin": origin} for name, origin in sorted(effective.items())]


async def load_user_rows(
    db: AsyncSession, ecosystem: str, normalized_name: str, version: str
) -> list[tuple[str, str, bool]]:
    rows = (
        await db.scalars(
            select(UserCapability).where(
                UserCapability.ecosystem == ecosystem,
                UserCapability.name == normalized_name,
                UserCapability.version.in_(["", version]),
            )
        )
    ).all()
    return [(row.capability, row.version, row.present) for row in rows]


async def current_classification(db: AsyncSession, version_id) -> Classification | None:
    return await db.scalar(
        select(Classification).where(
            Classification.version_id == version_id,
            Classification.classifier_version == CLASSIFIER_VERSION,
        )
    )


async def classification_block(db: AsyncSession, package: Package, version: PackageVersion) -> dict | None:
    """Card payload for one version's verdict; None when not classified yet."""
    classification = await current_classification(db, version.id)
    if classification is None:
        return None

    facets = (
        await db.scalars(
            select(ClassificationFacet).where(ClassificationFacet.classification_id == classification.id)
        )
    ).all()
    capabilities = (
        await db.scalars(
            select(ClassificationCapability).where(
                ClassificationCapability.classification_id == classification.id
            )
        )
    ).all()
    user_rows = await load_user_rows(db, package.ecosystem.value, package.normalized_name, version.version)
    annotations = (
        await db.scalars(
            select(UserCapability).where(
                UserCapability.ecosystem == package.ecosystem.value,
                UserCapability.name == package.normalized_name,
            )
        )
    ).all()
    relations = (
        await db.scalars(select(PackageRelation).where(PackageRelation.package_id == package.id))
    ).all()

    return {
        "classifier_version": classification.classifier_version,
        "classified_at": classification.classified_at,
        "version": version.version,
        "status": classification.status,
        "status_reason": classification.status_reason,
        "facets": [
            {"role": f.role, "confidence": f.confidence, "evidence": f.evidence, "provisional": f.provisional}
            for f in facets
        ],
        "capabilities": [
            {"capability": c.capability, "confidence": c.confidence, "evidence": c.evidence}
            for c in capabilities
        ],
        "effective_capabilities": merge_effective([c.capability for c in capabilities], user_rows),
        "annotations": [
            {
                "capability": a.capability,
                "present": a.present,
                "version": a.version,
                "note": a.note,
                "created_at": a.created_at,
            }
            for a in annotations
        ],
        "relations": [{"relation": r.relation, "name": r.related_name} for r in relations],
    }


# --- SQL-предикаты для фильтров списка (коррелируют с Package) --------------


def _latest_classification_join():
    return and_(
        PackageVersion.package_id == Package.id,
        PackageVersion.version == Package.latest_version,
        Classification.version_id == PackageVersion.id,
        Classification.classifier_version == CLASSIFIER_VERSION,
    )


def role_condition(role: str):
    return exists(
        select(1)
        .select_from(PackageVersion)
        .join(Classification, Classification.version_id == PackageVersion.id)
        .join(ClassificationFacet, ClassificationFacet.classification_id == Classification.id)
        .where(_latest_classification_join(), ClassificationFacet.role == role)
    )


def status_condition(status: str):
    return exists(
        select(1)
        .select_from(PackageVersion)
        .join(Classification, Classification.version_id == PackageVersion.id)
        .where(_latest_classification_join(), Classification.status == status)
    )


def _user_cap_exists(capability: str, scope, present: bool):
    return exists(
        select(1)
        .select_from(UserCapability)
        .where(
            UserCapability.ecosystem == cast(Package.ecosystem, String),
            UserCapability.name == Package.normalized_name,
            UserCapability.capability == capability,
            scope,
            UserCapability.present.is_(present),
        )
    )


def effective_cap_condition(capability: str):
    """SQL-версия merge_effective для latest-версии пакета:
    specific-пометка > пометка '' > машинный факт."""
    machine = exists(
        select(1)
        .select_from(PackageVersion)
        .join(Classification, Classification.version_id == PackageVersion.id)
        .join(ClassificationCapability, ClassificationCapability.classification_id == Classification.id)
        .where(_latest_classification_join(), ClassificationCapability.capability == capability)
    )
    spec_true = _user_cap_exists(capability, UserCapability.version == Package.latest_version, True)
    spec_false = _user_cap_exists(capability, UserCapability.version == Package.latest_version, False)
    all_true = _user_cap_exists(capability, UserCapability.version == "", True)
    all_false = _user_cap_exists(capability, UserCapability.version == "", False)
    return or_(spec_true, and_(not_(spec_false), or_(all_true, and_(not_(all_false), machine))))


async def registry_presence(db: AsyncSession, package: Package) -> list[dict]:
    rows = (
        await db.execute(
            select(PackageRegistry, Registry)
            .join(Registry, Registry.id == PackageRegistry.registry_id)
            .where(PackageRegistry.package_id == package.id)
        )
    ).all()
    return [
        {
            "slug": registry.slug,
            "registry_type": registry.registry_type.value,
            "is_primary": link.is_primary,
            "name_collision": link.name_collision,
            "checksum_mismatch": link.checksum_mismatch,
        }
        for link, registry in rows
    ]
