"""Which sources of a profile a pool of one distro takes (SPEC-MULTI-ECOSYSTEM.md §9.8.2, Q25): the distro's own, and
for an add-on repository (EPEL) those of its base distro, `config.base_distro` naming candidates in order (the first
one the profile has wins: EPEL 9 on Rocky 9, Alma 9 or RHEL 9)."""

from collections.abc import Sequence

from app.ecosystems.base import RegistryConfig, SourceProfile


def namespace_of(registry: RegistryConfig) -> str:
    return str(registry.config.get("namespace") or "")


def pool_sources(profile: SourceProfile, distro: str) -> list[RegistryConfig]:
    mine = [r for r in profile.registries if namespace_of(r) == distro]
    present = {namespace_of(r) for r in profile.registries}
    bases: list[str] = []
    for registry in mine:
        candidates = registry.config.get("base_distro") or []
        for base in candidates if isinstance(candidates, list) else []:
            if str(base) in present and str(base) != distro:
                if str(base) not in bases:
                    bases.append(str(base))
                break
    return mine + [r for r in profile.registries if namespace_of(r) in bases]


def namespaces_by_slug(registries: Sequence[RegistryConfig]) -> dict[str, str]:
    return {r.slug: namespace_of(r) for r in registries}
