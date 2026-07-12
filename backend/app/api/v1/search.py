from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_viewer
from app.db import get_db_session
from fastapi import HTTPException

from app.api.v1.packages import PackageSort, _parse_caps_param, _query_packages
from app.classifier import ROLES, STATUSES
from app.schemas.search import SearchResponse

router = APIRouter()


@router.get("/search", response_model=SearchResponse, dependencies=[Depends(require_viewer)])
async def search_packages(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db_session),
    ecosystem: str | None = Query(default=None),
    license: str | None = Query(default=None),
    has_vulnerabilities: bool | None = Query(default=None),
    updated_after: datetime | None = Query(default=None),
    role: str | None = Query(default=None),
    status: str | None = Query(default=None),
    cap_all: str | None = Query(default=None),
    cap_none: str | None = Query(default=None),
    sort: PackageSort = Query(default=PackageSort.name),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> SearchResponse:
    if role is not None and role not in ROLES:
        raise HTTPException(status_code=422, detail=f"unknown role '{role}'")
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"unknown status '{status}'")
    data, next_cursor, has_more = await _query_packages(
        db=db,
        q=q,
        license=license,
        has_vulnerabilities=has_vulnerabilities,
        updated_after=updated_after,
        sort=sort,
        limit=limit,
        cursor=cursor,
        ecosystem=ecosystem,
        role=role,
        status=status,
        cap_all=_parse_caps_param(cap_all, "cap_all"),
        cap_none=_parse_caps_param(cap_none, "cap_none"),
    )
    return SearchResponse(data=data, cursor=next_cursor, has_more=has_more)
