"""The crates plugin (SPEC-MULTI-ECOSYSTEM.md §3). Registered on import; see ecosystems.registry."""

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
from app.ecosystems.crates.adapters.sparse import CratesSparseAdapter
from app.ecosystems.crates.coordinates import CratesCoordinates
from app.ecosystems.crates.environment import CratesEnvironment, dependency_applies, parse_environment
from app.ecosystems.crates.versions import CratesVersionScheme
from app.ecosystems.merge import merge_by_priority
from app.ecosystems.registry import register
from app.ecosystems.routing import route


def _same_crate(ref: PackageRef, pattern: str) -> bool:
    """Rules list crate names as published; crates.io has no two names that differ only in case or `-`/`_`."""
    def fold(name: str) -> str:
        return name.strip().lower().replace("_", "-")

    return fold(ref.name) == fold(pattern)


class CratesPlugin:
    ecosystem: Ecosystem = "crates"
    purl_type: str = "cargo"

    def __init__(self) -> None:
        self.coordinates: Coordinates = CratesCoordinates()
        self.versions: VersionScheme = CratesVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "cargo-sparse":
            return CratesSparseAdapter(registry)
        raise ValueError(f"crates plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        return route(ref, profile.registries, "packages", _same_crate)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        crates_env = env if isinstance(env, CratesEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, crates_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        # A dependency from another registry (`registry = "..."`) is not in this ecosystem's profile.
        return None if (dep.details or {}).get("registry") else dep.target

    def resolver(self) -> Resolver:
        from app.ecosystems.crates.resolver import RESOLVER   # G5

        return RESOLVER


PLUGIN = CratesPlugin()
register(PLUGIN)
