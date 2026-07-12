from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_viewer
from app.db import get_db_session
from app.api.v1.packages import PackageSort, _query_packages
from app.schemas.search import SearchResponse

router = APIRouter()


@router.get("/search", response_model=SearchResponse, dependencies=[Depends(require_viewer)])
async def search_packages(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db_session),
    license: str | None = Query(default=None),
    has_vulnerabilities: bool | None = Query(default=None),
    updated_after: datetime | None = Query(default=None),
    sort: PackageSort = Query(default=PackageSort.name),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> SearchResponse:
    data, next_cursor, has_more = await _query_packages(
        db=db,
        q=q,
        license=license,
        has_vulnerabilities=has_vulnerabilities,
        updated_after=updated_after,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )
    return SearchResponse(data=data, cursor=next_cursor, has_more=has_more)
