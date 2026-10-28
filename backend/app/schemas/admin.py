from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Ecosystem
from app.schemas.updates import UpdateRunInfo


class SeedRequest(BaseModel):
    packages: list[str] | None = None


class RetryFailedRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=5000)


class FetchPackagesRequest(BaseModel):
    purls: list[str] = Field(min_length=1, max_length=500)


class AdminTaskResponse(BaseModel):
    task_id: str
    status: str
    operation: str


class PresenceProbeRequest(BaseModel):
    ecosystem: str = "pypi"


class RunInfo(BaseModel):
    """A row of the runs journal (IMPL R1)."""

    model_config = ConfigDict(from_attributes=True)

    operation: str
    ecosystem: Ecosystem | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    packages_total: int
    packages_checked: int
    packages_changed: int
    packages_updated: int
    packages_failed: int
    packages_unavailable: int
    events_created: int
    error: str | None


class SyncStatusResponse(BaseModel):
    status: str
    sync_type: str
    last_success_at: str | None
    last_error: str | None
    packages_synced: int
    packages_total: int
    cursor_data: dict
    last_update_run: UpdateRunInfo | None = None
    recent_runs: list[RunInfo] = Field(default_factory=list)
