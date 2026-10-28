"""Routing rules of registries (SPEC-MULTI-ECOSYSTEM.md §6.2), shared by plugins.

`registries.routing` is `{"mode": ..., <patterns_key>: [...]}`; the patterns key is the plugin's (`packages` for
PyPI, `scopes` for npm, `group_patterns` for Maven). Patterns describe which packages a source serves, so with
patterns every mode serves only matching packages; `block` inverts that.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast, get_args

from app.ecosystems.base import PackageRef, RegistryConfig

Mode = Literal["allow", "block", "exclusive", "priority"]
_MODES: frozenset[str] = frozenset(get_args(Mode))

# (package, pattern) -> does the pattern match the package
Matcher = Callable[[PackageRef, str], bool]


@dataclass(frozen=True, slots=True)
class Rule:
    mode: Mode | None            # None: no rule, the source serves any package
    patterns: tuple[str, ...]


def parse_rule(routing: Mapping[str, object] | None, patterns_key: str) -> Rule:
    if not routing:
        return Rule(None, ())
    unknown = sorted(set(routing) - {"mode", patterns_key})
    if unknown:
        # A misspelt key would otherwise silently mean "serves every package".
        raise ValueError(f"unknown routing keys {unknown}; expected 'mode' and {patterns_key!r}")
    raw = routing.get(patterns_key)
    if raw is None:
        patterns: tuple[str, ...] = ()
    elif isinstance(raw, list | tuple) and all(isinstance(p, str) for p in raw):
        patterns = tuple(cast(Sequence[str], raw))
    else:
        raise ValueError(f"routing.{patterns_key} must be a list of strings")
    mode = routing.get("mode")
    if mode is None:
        return Rule("allow" if patterns else None, patterns)
    if mode not in _MODES:
        raise ValueError(f"unknown routing mode {mode!r}")
    if mode in ("allow", "block", "exclusive") and not patterns:
        raise ValueError(f"routing mode {mode!r} needs routing.{patterns_key}")
    return Rule(cast(Mode, mode), patterns)


def is_priority(registry: RegistryConfig) -> bool:
    """`priority` sources are probed first in discovery: where the package exists, other sources are ignored."""
    return registry.routing is not None and registry.routing.get("mode") == "priority"


def route(
    ref: PackageRef, registries: Sequence[RegistryConfig], patterns_key: str, matches: Matcher
) -> list[RegistryConfig]:
    """Registries that may serve `ref`, in the given (profile) order.

    `allow`, `exclusive` and `priority` with patterns keep a source only for matching packages, `block` only for
    the others. A matching `exclusive` rule drops every source without one, even when the package is missing there.
    """
    kept: list[RegistryConfig] = []
    exclusive: list[RegistryConfig] = []
    for registry in registries:
        rule = parse_rule(registry.routing, patterns_key)
        matched = any(matches(ref, pattern) for pattern in rule.patterns)
        if rule.mode == "block":
            if not matched:
                kept.append(registry)
        elif rule.mode is None or (rule.mode == "priority" and not rule.patterns) or matched:
            kept.append(registry)
            if rule.mode == "exclusive":
                exclusive.append(registry)
    return exclusive or kept
