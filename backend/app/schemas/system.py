from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class RegistryStats(BaseModel):
    slug: str
    enabled: bool
    packages: int
    versions: int
    last_success_at: str | None
    degraded: bool


class EcosystemStats(BaseModel):
    ecosystem: str
    packages: int
    versions: int
    dependencies: int
    last_sync_at: str | None
    registries: list[RegistryStats] = Field(default_factory=list)


class StatsResponse(BaseModel):
    """The top-level counters are PyPI's, as before; `ecosystems` has every ecosystem."""

    ecosystem: str
    packages: int
    versions: int
    dependencies: int
    last_sync_at: str | None
    ecosystems: list[EcosystemStats] = Field(default_factory=list)
