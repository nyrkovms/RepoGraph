"""Typed shapes of the jsonb columns (SPEC-MULTI-ECOSYSTEM.md §5.4).

Plugins validate what they write into dependencies.environment_markers / details and package_versions.engines /
raw_metadata with these models, so the generic tables do not drift into free-form json.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarkerCondition(_Strict):
    marker: str
    op: str
    value: str


class PythonEnvironmentMarkers(_Strict):
    """PEP 508 markers (pypi)."""

    type: Literal["python_environment"] = "python_environment"
    expression: str
    conditions: list[MarkerCondition]


class Engines(_Strict):
    """Runtime constraints of a version; one key per runtime the ecosystem knows."""

    model_config = ConfigDict(extra="allow", frozen=True)

    python: str | None = None
    node: str | None = None
    rust: str | None = None
    go: str | None = None
    java: str | None = None
