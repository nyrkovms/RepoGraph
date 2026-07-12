from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class ClassificationStats(BaseModel):
    classifier_version: str
    total: int
    by_status: dict[str, int]
    roles: dict[str, int]
    effective_capabilities: dict[str, int]


class StatsResponse(BaseModel):
    ecosystem: str
    packages: int
    versions: int
    dependencies: int
    last_sync_at: str | None
    classification: ClassificationStats | None = None
