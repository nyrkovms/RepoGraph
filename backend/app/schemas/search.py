from pydantic import BaseModel

from app.schemas.packages import PackageListItem


class SearchResponse(BaseModel):
    data: list[PackageListItem]
    cursor: str | None
    has_more: bool
