"""Maven environment for the tree and resolve (SPEC-MULTI-ECOSYSTEM.md §7.3, §9.7): scopes, Java version, strategy.

`java_version` decides the profiles a POM activates by `<jdk>` (§7.3; default 21, the JDK the references ran on).
`scopes` are the scopes of the resolved graph (default `compile,runtime`, what a runtime classpath holds), `strategy`
`maven` (nearest wins, Maven 3.9) or `gradle` (highest wins), `plugins` Gradle plugins as `<id>:<version>` (resolved
through their marker artifacts). Dependencies carry their Maven scope in `scope` and their exclusions, type and
classifier in `details`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.ecosystems.base import InvalidVersion, NormalizedDependency
from app.ecosystems.maven.versions import MavenVersionScheme

SCOPES = ("compile", "runtime", "provided", "system", "test")
DEFAULT_SCOPES = ("compile", "runtime")
DEFAULT_JAVA = "21"
STRATEGIES = ("maven", "gradle")


@dataclass(frozen=True, slots=True)
class MavenEnvironment:
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    java_version: str = DEFAULT_JAVA
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
    return MavenEnvironment(scopes, str(java) if java is not None else DEFAULT_JAVA, strategy, tuple(plugins))


def _items(raw: object) -> tuple[str, ...]:
    items: Sequence[object] = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, Sequence) else ()
    return tuple(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def jdk_activates(spec: str, java: str) -> bool:
    """`JdkVersionProfileActivator`: `!` negates, a range holds the version, anything else is a prefix of it."""
    spec = spec.strip()
    if spec.startswith("!"):
        return not jdk_activates(spec[1:], java)
    if spec.startswith(("[", "(")):
        try:
            return MavenVersionScheme().constraint(spec).contains(java)
        except InvalidVersion:
            return False
    return java.startswith(spec)


def jdk_applies(dep: NormalizedDependency, java: str) -> bool:
    """A dependency of a jdk-activated profile needs that profile active; an active-by-default one, none of them."""
    markers = dep.environment_markers or {}
    spec = markers.get("jdk")
    if isinstance(spec, str):
        return jdk_activates(spec, java)
    unless = markers.get("unless_jdk")
    return not (isinstance(unless, list) and any(jdk_activates(str(s), java) for s in unless))


def maven_scope(dep: NormalizedDependency) -> str:
    return dep.scope if dep.scope in SCOPES else "compile"


def dependency_applies(dep: NormalizedDependency, env: MavenEnvironment, include_optional: bool) -> bool:
    """For the tree: a dependency whose scope the environment asks for; optional ones on request."""
    if dep.dependency_type == "optional" and not include_optional:
        return False
    return maven_scope(dep) in env.scopes and jdk_applies(dep, env.java_version)
