"""crates environment for the tree and resolve (SPEC-MULTI-ECOSYSTEM.md §7.3): target, features, default features.

The target selects platform-specific dependencies (cfg.py); features and `default_features` say which optional
dependencies of the root are on. The adapter records, for each optional dependency, every feature of the crate that
turns it on directly or through other features (`details.enabled_by`, `default` included when it does), so the tree
needs no feature map of its own.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.ecosystems.base import NormalizedDependency
from app.ecosystems.crates.cfg import cfg_of, platform_matches

DEFAULT_TARGET = "x86_64-unknown-linux-gnu"


@dataclass(frozen=True, slots=True)
class CratesEnvironment:
    target: str = DEFAULT_TARGET
    features: tuple[str, ...] = ()
    default_features: bool = True
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)

    def active_features(self) -> frozenset[str]:
        return frozenset(self.features) | ({"default"} if self.default_features else set())


def _features(value: object) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    items: Sequence[object] = value.split(",") if isinstance(value, str) else value if isinstance(value, Sequence) else ()
    return tuple(sorted({str(item).strip() for item in items if str(item).strip()}))


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def parse_environment(raw: Mapping[str, object]) -> CratesEnvironment:
    """`target` (a triple from targets.json; ValueError for others), `features` (a list or comma-separated) and
    `default_features`; missing values default to x86_64-unknown-linux-gnu with default features."""
    values = {key: value for key, value in raw.items() if value not in (None, "")}
    if not values:
        return CratesEnvironment(specified=False)
    target = str(values.get("target") or DEFAULT_TARGET).strip()
    cfg_of(target)
    return CratesEnvironment(target, _features(values.get("features")), _flag(values.get("default_features", True)))


def dependency_applies(dep: NormalizedDependency, env: CratesEnvironment, include_optional: bool) -> bool:
    """For the tree: normal and build dependencies for the target; optional ones when a feature of the environment
    turns them on, or all of them on request. Dev dependencies are for the crate's own tests."""
    if dep.dependency_type not in ("runtime", "build", "optional"):
        return False
    rust_cfg = (dep.environment_markers or {}).get("rust_cfg")
    if not platform_matches(rust_cfg if isinstance(rust_cfg, str) else None, env.target):
        return False
    if dep.dependency_type != "optional":
        return True
    enabled_by = (dep.details or {}).get("enabled_by")
    features = enabled_by if isinstance(enabled_by, list) else []
    return include_optional or bool(env.active_features() & {str(feature) for feature in features})
