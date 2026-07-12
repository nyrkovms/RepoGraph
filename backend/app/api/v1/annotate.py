"""Curation API: user capability annotations (classifire invariant #3 —
the only non-reproducible data; upsert by ecosystem+name+version+capability).
Writes require the admin token; annotations are readable via the package card."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_admin
from app.classifier import CAPABILITIES
from app.db import get_db_session
from app.fetcher.pipeline import find_package
from app.models.classification import UserCapability
from app.normalizers.names import normalize_name
from app.schemas.annotate import AnnotateDeleteRequest, AnnotateRequest, AnnotateResponse

router = APIRouter()


def _validate_capability(capability: str) -> None:
    if capability not in CAPABILITIES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown capability '{capability}'; allowed: {', '.join(CAPABILITIES)}",
        )


@router.post("/annotate", response_model=AnnotateResponse, dependencies=[Depends(require_admin)])
async def annotate(payload: AnnotateRequest, db: AsyncSession = Depends(get_db_session)) -> AnnotateResponse:
    _validate_capability(payload.capability)
    normalized = normalize_name(payload.ecosystem, payload.name)
    package = await find_package(db, payload.ecosystem, normalized)
    if package is None:
        raise HTTPException(status_code=404, detail=f"package '{normalized}' is not in the catalog")

    stmt = insert(UserCapability).values(
        ecosystem=payload.ecosystem,
        name=normalized,
        version=payload.version,
        capability=payload.capability,
        present=payload.present,
        note=payload.note,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["ecosystem", "name", "version", "capability"],
        set_={"present": payload.present, "note": payload.note},
    )
    await db.execute(stmt)
    await db.commit()
    return AnnotateResponse(ok=True)


@router.post("/annotate/delete", response_model=AnnotateResponse, dependencies=[Depends(require_admin)])
async def annotate_delete(
    payload: AnnotateDeleteRequest, db: AsyncSession = Depends(get_db_session)
) -> AnnotateResponse:
    normalized = normalize_name(payload.ecosystem, payload.name)
    result = await db.execute(
        delete(UserCapability).where(
            UserCapability.ecosystem == payload.ecosystem,
            UserCapability.name == normalized,
            UserCapability.version == payload.version,
            UserCapability.capability == payload.capability,
        )
    )
    await db.commit()
    return AnnotateResponse(ok=True, deleted=result.rowcount)


@router.get("/annotations", dependencies=[Depends(require_admin)])
async def list_annotations(db: AsyncSession = Depends(get_db_session)) -> list[dict]:
    rows = (
        await db.scalars(select(UserCapability).order_by(UserCapability.ecosystem, UserCapability.name))
    ).all()
    return [
        {
            "ecosystem": r.ecosystem,
            "name": r.name,
            "version": r.version,
            "capability": r.capability,
            "present": r.present,
            "note": r.note,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
