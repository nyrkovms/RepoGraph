"""Facts endpoint for the proxy's check system: facts only, no verdicts."""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_viewer
from app.classifier.facts import FactsUnavailable, ensure_facts
from app.config import get_settings
from app.db import get_db_session
from app.schemas.packages import ClassificationBlock, RegistryPresenceItem

router = APIRouter()

_ERROR_STATUS = {"not_found": 404, "auth_error": 502, "upstream_error": 503}


class FactsResponse(BaseModel):
    ecosystem: str
    name: str
    namespace: str | None
    normalized_name: str
    latest_version: str
    synced_at: datetime
    deprecated: bool
    deprecation_message: str | None
    version_status: str
    classification: ClassificationBlock | None
    registries: list[RegistryPresenceItem]


@router.get("/facts/{ecosystem}/{name:path}", response_model=FactsResponse, dependencies=[Depends(require_viewer)])
async def get_facts(
    ecosystem: str,
    name: str,
    refresh: bool = Query(default=False),
    db: AsyncSession = Depends(get_db_session),
) -> FactsResponse:
    if ecosystem not in ("pypi", "npm"):
        raise HTTPException(status_code=422, detail="ecosystem must be pypi or npm")

    settings = get_settings()
    try:
        payload = await asyncio.wait_for(
            ensure_facts(db, ecosystem, name, refresh=refresh),
            timeout=settings.facts_timeout_seconds,
        )
    except FactsUnavailable as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.kind, 503), detail=exc.detail) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail=f"facts assembly exceeded {settings.facts_timeout_seconds}s"
        ) from exc
    return FactsResponse(**payload)
