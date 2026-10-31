"""RPM environment (SPEC-MULTI-ECOSYSTEM.md §7.3, §9.8): the distro, the arch and which repositories, weak
dependencies and module streams take part."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field

from app.ecosystems.base import NormalizedDependency

ARCHES = ("x86_64", "aarch64", "ppc64le", "s390x")


@dataclass(frozen=True, slots=True)
class RpmEnvironment:
    distro: str | None = None                      # namespace of the sources (`rocky-9`); the root's when not given
    arch: str = "x86_64"
    repos: tuple[str, ...] = ()                    # source slugs; all of the distro's in the profile when empty
    weak_deps: bool = True                         # dnf's install_weak_deps default
    module_streams: Mapping[str, str] = field(default_factory=dict)
    disabled_modules: tuple[str, ...] = ()
    module_install: tuple[str, ...] = ()           # `nodejs:18/common`
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)


def _items(raw: object) -> tuple[str, ...]:
    items: Sequence[object] = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else ()
    return tuple(dict.fromkeys(str(i).strip() for i in items if str(i).strip()))


def parse_environment(raw: Mapping[str, object]) -> RpmEnvironment:
    values = {k: v for k, v in raw.items() if v not in (None, "", [], (), {})}
    if not values:
        return RpmEnvironment(specified=False)
    arch = str(values.get("arch", "x86_64"))
    if arch not in ARCHES:
        raise ValueError(f"unknown arch {arch!r}: use {', '.join(ARCHES)}")
    streams_raw = values.get("module_streams", {})
    if not isinstance(streams_raw, Mapping):
        raise ValueError("module_streams is an object: {\"nodejs\": \"18\"}")
    weak = values.get("weak_deps", True)
    return RpmEnvironment(
        distro=str(values["distro"]) if "distro" in values else None, arch=arch, repos=_items(values.get("repos")),
        weak_deps=weak if isinstance(weak, bool) else str(weak).lower() not in ("false", "0", "no"),
        module_streams={str(k): str(v) for k, v in streams_raw.items()},
        disabled_modules=_items(values.get("disabled_modules")), module_install=_items(values.get("module_install")),
    )


def dependency_applies(dep: NormalizedDependency, env: RpmEnvironment, include_optional: bool) -> bool:
    """For the tree: weak dependencies only on request (and with weak_deps), arch-specific ones for their arch."""
    if dep.dependency_type == "optional" and not (include_optional and env.weak_deps):
        return False
    arches = (dep.environment_markers or {}).get("rpm_arch")
    return not isinstance(arches, list) or env.arch in arches or "noarch" in arches
