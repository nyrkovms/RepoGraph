"""The NuGet plugin (SPEC-MULTI-ECOSYSTEM.md §3). Registered on import; see ecosystems.registry."""

from collections.abc import Mapping, Sequence

from app.ecosystems.base import (
    Coordinates,
    Ecosystem,
    Environment,
    MergedPackage,
    NormalizedDependency,
    NormalizedPackage,
    PackageRef,
    RegistryConfig,
    Resolver,
    SourceAdapter,
    SourceProfile,
    VersionScheme,
)
from app.ecosystems.nuget.adapters.v3 import NuGetV3Adapter
from app.ecosystems.nuget.coordinates import NuGetCoordinates
from app.ecosystems.nuget.environment import NuGetEnvironment, dependency_applies, parse_environment
from app.ecosystems.nuget.versions import NuGetVersionScheme
from app.ecosystems.merge import merge_by_priority
from app.ecosystems.registry import register
from app.ecosystems.routing import route


def _same_id(ref: PackageRef, pattern: str) -> bool:
    """Rules list package ids, case-insensitively, or id prefixes ending in `*` (`Contoso.*`), like package source
    mapping in nuget.config."""
    wanted = pattern.strip().lower()
    return ref.name.startswith(wanted[:-1]) if wanted.endswith("*") else ref.name == wanted


class NuGetPlugin:
    ecosystem: Ecosystem = "nuget"
    purl_type: str = "nuget"

    def __init__(self) -> None:
        self.coordinates: Coordinates = NuGetCoordinates()
        self.versions: VersionScheme = NuGetVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "nuget-v3":
            return NuGetV3Adapter(registry)
        raise ValueError(f"nuget plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        return route(ref, profile.registries, "packages", _same_id)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        nuget_env = env if isinstance(env, NuGetEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, nuget_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        return dep.target

    def resolver(self) -> Resolver:
        from app.ecosystems.nuget.resolver import RESOLVER   # I5

        return RESOLVER


PLUGIN = NuGetPlugin()
register(PLUGIN)
