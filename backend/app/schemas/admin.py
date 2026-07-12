from pydantic import BaseModel, Field


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


class RegistryCreateRequest(BaseModel):
    ecosystem: str = Field(pattern="^(pypi|npm|maven|go|crates|nuget)$")
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern="^[a-z0-9][a-z0-9-]*$")
    protocol: str = Field(pattern="^(pypi-json|pypi-simple|npm)$")
    registry_type: str = Field(default="corporate", pattern="^(public|corporate|mirror)$")
    base_url: str = Field(min_length=1, max_length=2048)
    api_url: str | None = Field(default=None, max_length=2048)
    auth_config: dict | None = None
    enumerate_enabled: bool = False
    sync_enabled: bool = True
    sync_priority: int = 100


class RegistryUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    protocol: str | None = Field(default=None, pattern="^(pypi-json|pypi-simple|npm)$")
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    api_url: str | None = Field(default=None, max_length=2048)
    auth_config: dict | None = None
    enumerate_enabled: bool | None = None
    sync_enabled: bool | None = None
    sync_priority: int | None = None


class RegistryResponse(BaseModel):
    slug: str
    ecosystem: str
    name: str
    protocol: str
    registry_type: str
    base_url: str
    api_url: str | None
    has_auth: bool
    enumerate_enabled: bool
    sync_enabled: bool
    sync_priority: int


class RegistryTestResponse(BaseModel):
    ok: bool
    detail: str


class ProbeRequest(BaseModel):
    name: str = Field(min_length=1)
    version: str | None = None
    artifact_filename: str | None = None


class ProbeDeleteRequest(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    artifact_filename: str | None = None
