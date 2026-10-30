"""NuGet environment for the tree and resolve (SPEC-MULTI-ECOSYSTEM.md §7.3, §9.6): the project's target framework.

A version's dependencies come in groups by framework; the project takes the nearest group (frameworks.py). Each
dependency the adapter stores carries its group (`environment_markers.dotnet_target_framework`) and the frameworks of
all the version's groups (`details.groups`), so whether it applies can be decided one dependency at a time.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from app.ecosystems.base import NormalizedDependency
from app.ecosystems.nuget.frameworks import Framework, nearest, project, short_name

DEFAULT_FRAMEWORK = "net8.0"


@dataclass(frozen=True, slots=True)
class NuGetEnvironment:
    target_framework: str = DEFAULT_FRAMEWORK
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)

    @property
    def framework(self) -> Framework:
        return project(self.target_framework)


def parse_environment(raw: Mapping[str, object]) -> NuGetEnvironment:
    """`target_framework` (default net8.0, the reference cases' project); ValueError outside Q7."""
    value = raw.get("target_framework")
    if value in (None, ""):
        return NuGetEnvironment(specified=False)
    return NuGetEnvironment(short_name(project(str(value))))


def group_of(dep: NormalizedDependency) -> str | None:
    value = (dep.environment_markers or {}).get("dotnet_target_framework")
    return value if isinstance(value, str) else None


def groups_of(dep: NormalizedDependency) -> list[str | None]:
    listed = (dep.details or {}).get("groups")
    return [g if isinstance(g, str) else None for g in listed] if isinstance(listed, list) else [group_of(dep)]


def dependency_applies(dep: NormalizedDependency, env: NuGetEnvironment, include_optional: bool) -> bool:
    """The dependency belongs to the group the project's framework takes."""
    return group_of(dep) == nearest(env.framework, groups_of(dep))
