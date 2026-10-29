"""npm environment for the tree and resolve (SPEC-MULTI-ECOSYSTEM.md §7.3): Node.js version, os, cpu.

A version's `os`/`cpu` lists (kept in its engines as comma-joined npm syntax, `!` for exclusions) are checked the way
npm's checkPlatform does: an excluded value never matches; with any positive values the current one must be listed.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from app.ecosystems.base import NormalizedDependency

_OS_ALIASES = {"windows": "win32", "win": "win32", "macos": "darwin", "mac": "darwin", "osx": "darwin"}
_CPU_ALIASES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}


DEFAULT_NODE, DEFAULT_OS, DEFAULT_CPU = "20.0.0", "linux", "x64"


@dataclass(frozen=True, slots=True)
class NpmEnvironment:
    node_version: str = DEFAULT_NODE
    os: str = DEFAULT_OS
    cpu: str = DEFAULT_CPU
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)


def parse_environment(raw: Mapping[str, object]) -> NpmEnvironment:
    """Accepts `node_version` (or `node`), `os` (or `platform`) and `cpu` (or `arch`); missing values default to
    Node 20 on linux x64, the target the resolver reference cases use."""
    values = {key: str(value) for key, value in raw.items() if value not in (None, "")}
    if not values:
        return NpmEnvironment(specified=False)
    node = values.get("node_version") or values.get("node") or DEFAULT_NODE
    os_name = (values.get("os") or values.get("platform") or DEFAULT_OS).lower()
    cpu = (values.get("cpu") or values.get("arch") or DEFAULT_CPU).lower()
    return NpmEnvironment(node.lstrip("v"), _OS_ALIASES.get(os_name, os_name), _CPU_ALIASES.get(cpu, cpu))


def platform_allows(spec: str | None, value: str) -> bool:
    if not spec:
        return True
    entries = [entry.strip() for entry in spec.split(",") if entry.strip()]
    if f"!{value}" in entries:
        return False
    positive = [entry for entry in entries if not entry.startswith("!")]
    return not positive or value in positive


def version_allows(engines: Mapping[str, str] | None, env: NpmEnvironment) -> bool:
    """The version can be installed on `env` (os and cpu; `engines.node` is only a warning in npm)."""
    engines = engines or {}
    return platform_allows(engines.get("os"), env.os) and platform_allows(engines.get("cpu"), env.cpu)


def dependency_applies(dep: NormalizedDependency, env: NpmEnvironment, include_optional: bool) -> bool:
    """For the tree: runtime and (npm >= 7 installs them) non-optional peer dependencies; optional ones on request.
    Platform checks need the target version and happen where it is known (the resolver)."""
    if dep.dependency_type == "optional":
        return include_optional
    if dep.dependency_type == "peer":
        return not (dep.details or {}).get("optional")
    return dep.dependency_type == "runtime"
