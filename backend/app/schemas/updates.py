from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UpdateEventItem(BaseModel):
    id: int
    package: str
    version: str | None
    kind: str
    detected_at: datetime
    occurred_at: datetime | None
    details: dict[str, Any]


class UpdateEventListResponse(BaseModel):
    data: list[UpdateEventItem]
    cursor: str | None
    has_more: bool


class UpdateRunInfo(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    status: str
    packages_checked: int
    packages_changed: int
    packages_updated: int
    packages_failed: int
    packages_unavailable: int
    events_created: int


class UpdatesSummaryTotals(BaseModel):
    packages: int = 0
    releases: int = 0
    yanked: int = 0
    removed: int = 0
    vulnerabilities: int = 0
    unavailable: int = 0


class UpdatesSummaryPackage(BaseModel):
    name: str
    from_version: str | None
    to_version: str | None
    max_bump: str | None
    releases: list[str] = Field(default_factory=list)
    yanked: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    vulnerabilities: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    last_detected_at: datetime


class UpdatesSummaryResponse(BaseModel):
    since: datetime
    until: datetime
    last_run: UpdateRunInfo | None
    totals: UpdatesSummaryTotals
    packages: list[UpdatesSummaryPackage]
