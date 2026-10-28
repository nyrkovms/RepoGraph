"""The PyPI plugin (SPEC-MULTI-ECOSYSTEM.md §3). Registered on import; see ecosystems.registry."""

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
from app.ecosystems.pypi.adapters import WarehouseAdapter
from app.ecosystems.pypi.coordinates import PyPICoordinates
from app.ecosystems.pypi.environment import PythonEnvironment, dependency_applies, parse_environment
from app.ecosystems.pypi.versions import PyPIVersionScheme
from app.ecosystems.registry import register


class PyPIPlugin:
    ecosystem: Ecosystem = "pypi"
    purl_type: str = "pypi"

    def __init__(self) -> None:
        self.coordinates: Coordinates = PyPICoordinates()
        self.versions: VersionScheme = PyPIVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "pypi-warehouse":
            return WarehouseAdapter(registry)
        raise ValueError(f"PyPI plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        # Profile order only; routing modes (allow/block/exclusive/priority) arrive with ecosystems/routing.py (B2).
        return list(profile.registries)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        python_env = env if isinstance(env, PythonEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, python_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        return dep.target

    def resolver(self) -> Resolver:
        raise NotImplementedError("the PyPI resolver moves behind the contract with the graph format (E0-C2)")


PLUGIN = PyPIPlugin()
register(PLUGIN)
