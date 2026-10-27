from pydantic import BaseModel, Field

from app.schemas.updates import UpdateRunInfo


class SeedRequest(BaseModel):
    packages: list[str] | None = None


class RefreshRequest(BaseModel):
    limit: int = Field(default=500, ge=1, le=5000)


class RetryFailedRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=5000)


class AdminTaskResponse(BaseModel):
    task_id: str
    status: str
    operation: str


class SyncStatusResponse(BaseModel):
    status: str
    sync_type: str
    last_success_at: str | None
    last_error: str | None
    packages_synced: int
    packages_total: int
    cursor_data: dict
    last_update_run: UpdateRunInfo | None = None
