"""The Maven plugin (SPEC-MULTI-ECOSYSTEM.md §3). Registered on import; see ecosystems.registry."""

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
from app.ecosystems.maven.adapters.maven2 import Maven2Adapter
from app.ecosystems.maven.coordinates import MavenCoordinates
from app.ecosystems.maven.environment import MavenEnvironment, dependency_applies, parse_environment
from app.ecosystems.maven.routing import group_matches
from app.ecosystems.maven.versions import MavenVersionScheme
from app.ecosystems.merge import merge_by_priority
from app.ecosystems.registry import register
from app.ecosystems.routing import route


class MavenPlugin:
    ecosystem: Ecosystem = "maven"
    purl_type: str = "maven"

    def __init__(self) -> None:
        self.coordinates: Coordinates = MavenCoordinates()
        self.versions: VersionScheme = MavenVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "maven2":
            return Maven2Adapter(registry)
        raise ValueError(f"maven plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        return route(ref, profile.registries, "group_patterns", group_matches)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        maven_env = env if isinstance(env, MavenEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, maven_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        return dep.target

    def resolver(self) -> Resolver:
        from app.ecosystems.maven.resolver import RESOLVER   # J5

        return RESOLVER


PLUGIN = MavenPlugin()
register(PLUGIN)
