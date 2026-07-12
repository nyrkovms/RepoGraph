from __future__ import annotations

from pydantic import BaseModel, Field


class ResolveDependencyInput(BaseModel):
    name: str
    version_spec: str = "*"
    extras: list[str] = Field(default_factory=list)


class ResolveEnvironment(BaseModel):
    python_version: str = "3.12"
    python_full_version: str | None = None
    sys_platform: str = "linux"
    os_name: str = "posix"
    platform_machine: str = "x86_64"
    platform_system: str = "Linux"
    implementation_name: str = "cpython"


class ResolveOptions(BaseModel):
    include_dev: bool = False
    max_depth: int = Field(default=50, ge=1, le=200)
    max_rounds: int = Field(default=200, ge=20, le=2000)


class ResolveRequest(BaseModel):
    ecosystem: str = "pypi"
    input_mode: str = "requirements"
    dependencies: list[ResolveDependencyInput] = Field(min_length=1)
    environment: ResolveEnvironment = Field(default_factory=ResolveEnvironment)
    options: ResolveOptions = Field(default_factory=ResolveOptions)


class ResolvedDependencyNode(BaseModel):
    name: str
    version: str
    constraint: str
    dependency_type: str | None = None
    extra_name: str | None = None
    environment_markers: dict | None = None
    dependencies: list["ResolvedDependencyNode"] = Field(default_factory=list)


ResolvedDependencyNode.model_rebuild()


class ResolvedPackage(BaseModel):
    version: str
    latest_compatible_version: str | None = None
    has_newer_compatible_version: bool = False
    dependencies: list[ResolvedDependencyNode]


class ResolveSuccessResponse(BaseModel):
    success: bool = True
    input_mode: str = "requirements"
    resolved: dict[str, ResolvedPackage]
    hidden_roots: list[str] = Field(default_factory=list)
    total_packages: int
    total_versions: int
    conflicts: list[dict] = Field(default_factory=list)
    resolve_strategy: str = "resolvelib"
    resolve_time_ms: float


class ErrorPayload(BaseModel):
    code: str
    message: str
    details: dict | None = None
    trace_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorPayload
