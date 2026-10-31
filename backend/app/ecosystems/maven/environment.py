"""Maven environment for the tree and resolve (SPEC-MULTI-ECOSYSTEM.md §7.3, §9.7): scopes, Java version, strategy.

`scopes` are the scopes of the resolved graph (default `compile,runtime`, what a runtime classpath holds), `strategy`
`maven` (nearest wins, Maven 3.9) or `gradle` (highest wins), `plugins` Gradle plugins as `<id>:<version>` (resolved
through their marker artifacts). Dependencies carry their Maven scope in `scope` and their exclusions, type and
classifier in `details`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.ecosystems.base import NormalizedDependency

SCOPES = ("compile", "runtime", "provided", "system", "test")
DEFAULT_SCOPES = ("compile", "runtime")
STRATEGIES = ("maven", "gradle")


@dataclass(frozen=True, slots=True)
class MavenEnvironment:
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    java_version: str | None = None
    strategy: str = "maven"
    plugins: tuple[tuple[str, str], ...] = ()
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)


def parse_environment(raw: Mapping[str, object]) -> MavenEnvironment:
    """`scopes` and `plugins` (lists or comma-separated), `java_version`, `strategy`; ValueError for unknown values."""
    values = {key: value for key, value in raw.items() if value not in (None, "", [], ())}
    if not values:
        return MavenEnvironment(specified=False)
    scopes_raw = values.get("scopes", DEFAULT_SCOPES)
    scopes = _items(scopes_raw)
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown or not scopes:
        raise ValueError(f"unknown Maven scopes {unknown or scopes_raw!r}: use {', '.join(SCOPES)}")
    strategy = str(values.get("strategy", "maven"))
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}: use maven or gradle")
    plugins: list[tuple[str, str]] = []
    for item in _items(values.get("plugins", ())):
        plugin_id, _, version = item.rpartition(":")
        if not plugin_id or not version:
            raise ValueError(f"Gradle plugin {item!r}: use <id>:<version>, e.g. org.springframework.boot:3.3.5")
        plugins.append((plugin_id, version))
    java = values.get("java_version")
    return MavenEnvironment(scopes, str(java) if java is not None else None, strategy, tuple(plugins))


def _items(raw: object) -> tuple[str, ...]:
    items: Sequence[object] = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, Sequence) else ()
    return tuple(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def maven_scope(dep: NormalizedDependency) -> str:
    return dep.scope if dep.scope in SCOPES else "compile"


def dependency_applies(dep: NormalizedDependency, env: MavenEnvironment, include_optional: bool) -> bool:
    """For the tree: a dependency whose scope the environment asks for; optional ones on request."""
    if dep.dependency_type == "optional" and not include_optional:
        return False
    return maven_scope(dep) in env.scopes
