from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class StatsResponse(BaseModel):
    ecosystem: str
    packages: int
    versions: int
    dependencies: int
    last_sync_at: str | None
