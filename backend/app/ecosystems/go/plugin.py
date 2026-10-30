"""The Go plugin (SPEC-MULTI-ECOSYSTEM.md §3). Registered on import; see ecosystems.registry."""

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
from app.ecosystems.go.adapters.goproxy import GoProxyAdapter
from app.ecosystems.go.coordinates import GoCoordinates
from app.ecosystems.go.environment import GoEnvironment, dependency_applies, parse_environment
from app.ecosystems.go.versions import GoVersionScheme
from app.ecosystems.merge import merge_by_priority
from app.ecosystems.registry import register
from app.ecosystems.routing import route


def _path_matches(ref: PackageRef, pattern: str) -> bool:
    """Rules list module path prefixes, like GOPRIVATE: `corp.example.com/*` or `github.com/corp` and below."""
    prefix = pattern.strip().rstrip("/").removesuffix("/*")
    return ref.name == prefix or ref.name.startswith(prefix + "/")


class GoPlugin:
    ecosystem: Ecosystem = "go"
    purl_type: str = "golang"

    def __init__(self) -> None:
        self.coordinates: Coordinates = GoCoordinates()
        self.versions: VersionScheme = GoVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "goproxy":
            return GoProxyAdapter(registry)
        raise ValueError(f"go plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        return route(ref, profile.registries, "modules", _path_matches)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        go_env = env if isinstance(env, GoEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, go_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        return dep.target

    def resolver(self) -> Resolver:
        from app.ecosystems.go.resolver import RESOLVER   # H5

        return RESOLVER


PLUGIN = GoPlugin()
register(PLUGIN)
