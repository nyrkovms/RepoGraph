from datetime import datetime

from pydantic import BaseModel, Field

# Shape checks (types, lengths) live here; what depends on the ecosystem's plugin (protocol, routing rules) and on
# other fields (credentials) is checked in services.registries so the API and the CLI report the same messages.
SLUG_PATTERN = r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$"


class RegistryCreate(BaseModel):
    """`enabled` is deliberately not a field: a new source starts disabled until it passes a connection test."""

    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    ecosystem: str
    protocol: str
    base_url: str = Field(min_length=1, max_length=2048)
    priority: int = Field(default=100, ge=0)
    config: dict = Field(default_factory=dict)
    routing: dict | None = None
    auth_type: str = "none"
    credentials_ref: str | None = None


class RegistryUpdate(BaseModel):
    """Partial update. slug, ecosystem and protocol are immutable: they are accepted only to be compared."""

    slug: str | None = None
    ecosystem: str | None = None
    protocol: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    priority: int | None = Field(default=None, ge=0)
    config: dict | None = None
    routing: dict | None = None
    auth_type: str | None = None
    credentials_ref: str | None = None


class RegistryState(BaseModel):
    status: str
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    degraded_until: datetime | None
    degraded: bool
    cursor_data: dict


class RegistryResponse(BaseModel):
    slug: str
    name: str
    ecosystem: str
    protocol: str | None
    base_url: str
    priority: int
    enabled: bool
    routing: dict | None
    config: dict
    auth_type: str
    credentials_ref: str | None
    state: RegistryState | None
    packages: int


class RegistryListResponse(BaseModel):
    data: list[RegistryResponse]


class ConnectionTestResponse(BaseModel):
    ok: bool
    error: str | None
    elapsed_ms: float


class ProfileMember(BaseModel):
    slug: str
    routing_override: dict | None = None


class ProfileUpsert(BaseModel):
    name: str = Field(min_length=1)
    is_default: bool = False
    registries: list[ProfileMember] = Field(default_factory=list)


class ProfileResponse(BaseModel):
    slug: str
    name: str
    ecosystem: str
    is_default: bool
    registries: list[ProfileMember]


class ProfileListResponse(BaseModel):
    data: list[ProfileResponse]


# --- import file (registries import) -----------------------------------------------------------------


class RegistryImport(RegistryCreate):
    """A registry of the import file. `enabled: true` takes effect only after a successful connection test."""

    enabled: bool | None = None


class ProfileImport(ProfileUpsert):
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    ecosystem: str


class ImportDocument(BaseModel):
    registries: list[RegistryImport] = Field(default_factory=list)
    profiles: list[ProfileImport] = Field(default_factory=list)
