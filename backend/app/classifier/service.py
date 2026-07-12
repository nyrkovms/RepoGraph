"""Classification service: registry loading, adapters and storage.

Invariants (from classifire):
- classifications are versioned by CLASSIFIER_VERSION; reclassify replaces
  only rows of the current version, history stays;
- user_capabilities are never touched by the classifier;
- plugin-of relations are rebuilt per classification, platform-payload rows
  are append-only (INSERT ... ON CONFLICT DO NOTHING).
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier import CLASSIFIER_VERSION
from app.classifier.core import ProbeView, Result
from app.classifier.npm import classify_npm, platform_payload_relations
from app.classifier.pypi import classify_pypi
from app.classifier.registries_data import ENTRY_GROUP_RULES, ROLE_OVERRIDES
from app.models.classification import (
    Classification,
    ClassificationCapability,
    ClassificationFacet,
    EntryGroupRule,
    PackageRelation,
    RoleOverride,
)
from app.models.dist_probe import DistProbe
from app.models.package import Package
from app.models.version import PackageVersion

logger = logging.getLogger(__name__)


async def seed_rule_registries(db: AsyncSession) -> None:
    """Idempotent: local curator edits are never overwritten."""
    for pattern, group_type, role_effect, note, host in ENTRY_GROUP_RULES:
        stmt = insert(EntryGroupRule).values(
            pattern=pattern, group_type=group_type, role_effect=role_effect, note=note, host=host
        )
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["pattern"]))
    for ecosystem, name, roles, note in ROLE_OVERRIDES:
        stmt = insert(RoleOverride).values(ecosystem=ecosystem, name=name, roles=roles, note=note)
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["ecosystem", "name"]))
    await db.flush()


async def load_overrides(db: AsyncSession, ecosystem: str) -> dict[str, list[str]]:
    rows = (await db.scalars(select(RoleOverride).where(RoleOverride.ecosystem == ecosystem))).all()
    return {row.name: list(row.roles) for row in rows}


async def load_entry_rules(db: AsyncSession) -> list[dict]:
    rows = (await db.scalars(select(EntryGroupRule))).all()
    return [
        {"pattern": r.pattern, "group_type": r.group_type, "role_effect": r.role_effect, "host": r.host}
        for r in rows
    ]


async def _representative_probe(db: AsyncSession, version_id) -> ProbeView | None:
    row = await db.scalar(
        select(DistProbe).where(DistProbe.version_id == version_id, DistProbe.is_representative.is_(True))
    )
    if row is None:
        return None
    return ProbeView(
        entry_points_txt=row.entry_points_txt,
        top_level_txt=row.top_level_txt,
        file_facts=row.file_facts,
        error=row.error,
        available=True,
    )


def build_manifest(package: Package, version: PackageVersion) -> dict:
    """classifire-shaped manifest per ecosystem.

    npm: the version manifest (stored per version).
    pypi: {"info", "urls"} — package-level info corresponds to the latest
    version, which is exactly the version we classify.
    """
    if package.ecosystem.value == "npm":
        return dict(version.raw_metadata or {})
    raw = package.raw_metadata or {}
    return {"info": raw.get("info") or {}, "urls": raw.get("urls") or []}


async def classify_one_version(db: AsyncSession, version: PackageVersion, package: Package) -> Result:
    ecosystem = package.ecosystem.value
    probe = await _representative_probe(db, version.id)
    manifest = build_manifest(package, version)

    if ecosystem == "npm":
        overrides = await load_overrides(db, "npm")
        result = classify_npm(package.normalized_name, manifest, probe, overrides)
        payload_relations = platform_payload_relations(manifest)
    else:
        overrides = await load_overrides(db, "pypi")
        entry_rules = await load_entry_rules(db)
        result = classify_pypi(package.normalized_name, manifest, probe, overrides, entry_rules)
        payload_relations = []

    await _store_relations(db, package.id, result, payload_relations)
    await _store_classification(db, version.id, result)
    return result


async def _store_relations(db: AsyncSession, package_id, result: Result, payload_relations: list) -> None:
    # plugin-of пересоздаётся при каждой классификации; platform-payload — дозаписывается
    await db.execute(
        delete(PackageRelation).where(
            PackageRelation.package_id == package_id, PackageRelation.relation == "plugin-of"
        )
    )
    seen: set[tuple[str, str]] = set()
    for relation, related_name in list(result.relations) + payload_relations:
        if (relation, related_name) in seen:
            continue
        seen.add((relation, related_name))
        stmt = insert(PackageRelation).values(
            package_id=package_id, related_name=related_name, relation=relation
        )
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["package_id", "related_name", "relation"]))


async def _store_classification(db: AsyncSession, version_id, result: Result) -> None:
    await db.execute(
        delete(Classification).where(
            Classification.version_id == version_id,
            Classification.classifier_version == CLASSIFIER_VERSION,
        )
    )
    row = Classification(
        version_id=version_id,
        classifier_version=CLASSIFIER_VERSION,
        classified_at=datetime.now(UTC),
        status=result.status,
        status_reason=result.status_reason,
        summary=(result.summary or "")[:500],
    )
    db.add(row)
    await db.flush()
    for facet in result.facets:
        db.add(
            ClassificationFacet(
                classification_id=row.id,
                role=facet.name,
                confidence=facet.confidence,
                evidence=facet.evidence,
                provisional=facet.provisional,
            )
        )
    for capability in result.capabilities:
        db.add(
            ClassificationCapability(
                classification_id=row.id,
                capability=capability.name,
                confidence=capability.confidence,
                evidence=capability.evidence,
            )
        )
    await db.flush()
