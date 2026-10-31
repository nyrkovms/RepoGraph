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

    def __init__(self) -> None:
        self.coordinates: Coordinates = RpmCoordinates()
        self.versions: VersionScheme = RpmVersionScheme()

    def adapter(self, registry: RegistryConfig) -> SourceAdapter:
        if registry.protocol == "rpm-repodata":
            return RepodataAdapter(registry)
        raise ValueError(f"rpm plugin has no adapter for protocol {registry.protocol!r} (registry {registry.slug})")

    def sources_for(self, ref: PackageRef, profile: SourceProfile) -> Sequence[RegistryConfig]:
        # A source is one repository of one release: only those of the package's distro (§6.4).
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
        """The package providing the capability in the profile's sources of its distro (§2.2): the worker knows the
        whole repositories. A capability that names a package provided by itself resolves to that package."""
        rpm_env = env if isinstance(env, RpmEnvironment) else parse_environment(env.as_dict())
        registries = [r for r in profile.registries if _namespace(r) == dep.target.namespace]
        if not registries:
            return None
        providers = await client.whatprovides(registries, rpm_env.arch, dep.raw_spec or dep.target.name)
        names = [str(p["name"]) for p in providers]
        if not names:
            return None
        chosen = dep.target.name if dep.target.name in names else sorted(names)[0]
        return PackageRef("rpm", dep.target.namespace, chosen)

    def resolver(self) -> Resolver:
        from app.ecosystems.rpm.resolver import RESOLVER   # K6

        return RESOLVER


PLUGIN = RpmPlugin()
register(PLUGIN)
