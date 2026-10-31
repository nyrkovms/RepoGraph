"""The RPM plugin (SPEC-MULTI-ECOSYSTEM.md §3, §2.2). Registered on import; see ecosystems.registry."""

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
from app.ecosystems.registry import register
from app.ecosystems.routing import route
from app.ecosystems.rpm import client
from app.ecosystems.rpm.adapters.repodata import RepodataAdapter
from app.ecosystems.rpm.coordinates import RpmCoordinates, package_matches
from app.ecosystems.rpm.environment import RpmEnvironment, dependency_applies, parse_environment
from app.ecosystems.rpm.versions import RpmVersionScheme


def _namespace(registry: RegistryConfig) -> str:
    return str(registry.config.get("namespace") or "")


class RpmPlugin:
    ecosystem: Ecosystem = "rpm"
    purl_type: str = "rpm"
    vanished_status: str = "superseded"            # a build that left the repository (Q3): kept, no event

    def __init__(self) -> None:
        self.coordinates: Coordinates = RpmCoordinates()
        self.versions: VersionScheme = RpmVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "rpm-repodata":
            return RepodataAdapter(registry)
        raise ValueError(f"rpm plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        # A source is one repository of one release: only those of the package's distro (§6.4). Every source's rule
        # is still parsed, so a broken one fails whatever the package.
        route(ref, profile.registries, "packages", package_matches)
        mine = [r for r in profile.registries if _namespace(r) == ref.namespace]
        return route(ref, mine, "packages", package_matches)

    def merge(self, per_source: Sequence[NormalizedPackage]) -> MergedPackage:
        return merge_by_priority(per_source)

    def parse_environment(self, raw: Mapping[str, object]) -> Environment:
        return parse_environment(raw)

    def dependency_applies(self, dep: NormalizedDependency, env: Environment, include_optional: bool) -> bool:
        rpm_env = env if isinstance(env, RpmEnvironment) else parse_environment(env.as_dict())
        return dependency_applies(dep, rpm_env, include_optional)

    async def dependency_target(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> PackageRef | None:
        found = await self.provider(dep, env, profile)
        return found[0] if found is not None else None

    async def provider(
        self, dep: NormalizedDependency, env: Environment, profile: SourceProfile
    ) -> tuple[PackageRef, str] | None:
        """The package providing the capability (with its constraint: `openssl-libs(x86-64) = 1:3.0.7-27.el9`) in the
        profile's sources of its distro, and the EVR of its newest providing build (§2.2: the worker knows the whole
        repositories). A capability named like a package it is provided by resolves to that package."""
        rpm_env = env if isinstance(env, RpmEnvironment) else parse_environment(env.as_dict())
        registries = [r for r in profile.registries if _namespace(r) == dep.target.namespace]
        if not registries:
            return None
        providers = await client.whatprovides(registries, rpm_env.arch, dep.raw_spec or dep.target.name)
        if not providers:
            return None
        names = [str(p["name"]) for p in providers]
        chosen = dep.target.name if dep.target.name in names else sorted(set(names))[0]
        newest = next(p for p in providers if p["name"] == chosen)          # the worker answers newest first
        return PackageRef("rpm", dep.target.namespace, chosen), str(newest["evr"])

    def resolver(self) -> Resolver:
        from app.ecosystems.rpm.resolver import RESOLVER   # K6

        return RESOLVER


PLUGIN = RpmPlugin()
register(PLUGIN)
