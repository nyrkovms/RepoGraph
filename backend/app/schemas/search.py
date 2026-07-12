from app.schemas.packages import PackageListItem
from pydantic import BaseModel


class SearchResponse(BaseModel):
    data: list[PackageListItem]
    cursor: str | None
    has_more: bool
