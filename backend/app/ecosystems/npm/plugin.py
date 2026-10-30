"""The npm plugin (SPEC-MULTI-ECOSYSTEM.md §3). Registered on import; see ecosystems.registry."""

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
from app.ecosystems.merge import merge_by_priority
from app.ecosystems.npm.adapters.registry import NpmRegistryAdapter
from app.ecosystems.npm.coordinates import NpmCoordinates
from app.ecosystems.npm.environment import NpmEnvironment, dependency_applies, parse_environment
from app.ecosystems.npm.routing import scope_matches
from app.ecosystems.npm.versions import NpmVersionScheme
from app.ecosystems.registry import register
from app.ecosystems.routing import route


class NpmPlugin:
    ecosystem: Ecosystem = "npm"
    purl_type: str = "npm"

    def __init__(self) -> None:
        self.coordinates: Coordinates = NpmCoordinates()
        self.versions: VersionScheme = NpmVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "npm-registry":
            return NpmRegistryAdapter(registry)
        raise ValueError(f"npm plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        # npm rules list scopes (`@corp/*` from the corporate registry), like scope registries in .npmrc.
        return route(ref, profile.registries, "scopes", scope_matches)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        npm_env = env if isinstance(env, NpmEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, npm_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        return None if (dep.details or {}).get("unresolvable") else dep.target

    def resolver(self) -> Resolver:
        from app.ecosystems.npm.resolver import RESOLVER   # F5

        return RESOLVER


PLUGIN = NpmPlugin()
register(PLUGIN)
