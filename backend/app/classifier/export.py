"""Dataset exports: annotations transfer (classifire format) and LLM-layer input."""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier import CAPABILITIES, CLASSIFIER_VERSION
from app.classifier.effective import load_user_rows, merge_effective
from app.models.classification import (
    Classification,
    ClassificationCapability,
    ClassificationFacet,
    UserCapability,
)
from app.models.package import Package
from app.models.version import PackageVersion
from app.models.version_description import VersionDescription


async def export_annotations(db: AsyncSession, output: Path) -> int:
    rows = (
        await db.scalars(select(UserCapability).order_by(UserCapability.ecosystem, UserCapability.name))
    ).all()
    with output.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(
                json.dumps(
                    {
                        "ecosystem": row.ecosystem,
                        "name": row.name,
                        "version": row.version,
                        "capability": row.capability,
                        "present": row.present,
                        "note": row.note,
                        "created_at": row.created_at.isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)


async def import_annotations(db: AsyncSession, source: Path, replace: bool = False) -> dict[str, int]:
    imported = 0
    skipped = 0
    invalid = 0
    with source.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            capability = record.get("capability")
            if capability not in CAPABILITIES or record.get("ecosystem") not in ("pypi", "npm"):
                invalid += 1
                continue
            values = {
                "ecosystem": record["ecosystem"],
                "name": record["name"],
                "version": record.get("version", ""),
                "capability": capability,
                "present": bool(record["present"]),
                "note": record.get("note") or "",
            }
            stmt = insert(UserCapability).values(**values)
            if replace:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ecosystem", "name", "version", "capability"],
                    set_={"present": values["present"], "note": values["note"]},
                )
                imported += 1
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["ecosystem", "name", "version", "capability"]
                )
            result = await db.execute(stmt)
            if not replace:
                if result.rowcount:
                    imported += 1
                else:
                    skipped += 1
    await db.commit()
    return {"imported": imported, "skipped": skipped, "invalid": invalid}


async def export_llm_input(db: AsyncSession, output: Path, max_chars: int = 8000) -> int:
    """Non-ok verdicts of the current rules version → JSONL input for the L2 layer."""
    rows = (
        await db.execute(
            select(Classification, PackageVersion, Package)
            .join(PackageVersion, PackageVersion.id == Classification.version_id)
            .join(Package, Package.id == PackageVersion.package_id)
            .where(
                Classification.classifier_version == CLASSIFIER_VERSION,
                Classification.status != "ok",
            )
            .order_by(Package.normalized_name)
        )
    ).all()

    exported = 0
    now = datetime.now(UTC).isoformat()
    with output.open("w", encoding="utf-8") as fh:
        for classification, version, package in rows:
            facets = (
                await db.scalars(
                    select(ClassificationFacet).where(
                        ClassificationFacet.classification_id == classification.id
                    )
                )
            ).all()
            capabilities = (
                await db.scalars(
                    select(ClassificationCapability).where(
                        ClassificationCapability.classification_id == classification.id
                    )
                )
            ).all()
            user_rows = await load_user_rows(
                db, package.ecosystem.value, package.normalized_name, version.version
            )
            annotations = (
                await db.scalars(
                    select(UserCapability).where(
                        UserCapability.ecosystem == package.ecosystem.value,
                        UserCapability.name == package.normalized_name,
                    )
                )
            ).all()

            descriptions: dict[str, str] = {}
            if package.description:
                descriptions["registry-summary"] = package.description[:max_chars]
            for desc in (
                await db.scalars(
                    select(VersionDescription).where(VersionDescription.version_id == version.id)
                )
            ).all():
                descriptions[desc.origin] = desc.content[:max_chars]

            fh.write(
                json.dumps(
                    {
                        "ecosystem": package.ecosystem.value,
                        "name": package.normalized_name,
                        "version": version.version,
                        "status": classification.status,
                        "status_reason": classification.status_reason,
                        "structural_roles": sorted({f.role for f in facets}),
                        "capabilities": merge_effective([c.capability for c in capabilities], user_rows),
                        "user_annotations": [
                            {
                                "capability": a.capability,
                                "present": a.present,
                                "version": a.version,
                                "note": a.note,
                            }
                            for a in annotations
                        ],
                        "summary": classification.summary,
                        "descriptions": descriptions,
                        "exported_at": now,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            exported += 1
    return exported
