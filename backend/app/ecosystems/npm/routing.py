"""npm routing patterns: `scopes` (SPEC-MULTI-ECOSYSTEM.md §6.2). Use as `route(ref, registries, "scopes", scope_matches)`."""

from app.ecosystems.base import PackageRef


def scope_matches(ref: PackageRef, pattern: str) -> bool:
    """`corp` or `@corp` (or `@corp/*`) matches every `@corp/*` package; `@corp/tool` matches that one package.

    Patterns follow the spelling of `.npmrc` (`@corp:registry=...`) and compare as written: npm names are lowercase.
    """
    text = pattern.strip().removeprefix("@")
    scope, slash, name = text.partition("/")
    if not scope or not ref.namespace or scope != ref.namespace:
        return False
    return not slash or name in ("*", ref.name)
