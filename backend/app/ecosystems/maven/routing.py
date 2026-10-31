"""Maven routing patterns: `group_patterns` (SPEC-MULTI-ECOSYSTEM.md §6.2). Use as `route(ref, registries, "group_patterns", group_matches)`."""

from app.ecosystems.base import PackageRef


def group_matches(ref: PackageRef, pattern: str) -> bool:
    """Gradle `includeGroup`-like: `com.android.tools` is that group only, `androidx.*` is `androidx` and every group
    below it (`androidx.core`, not `androidxyz`), `*` is every group. Groups are case-sensitive."""
    text = pattern.strip()
    group = ref.namespace
    if not text or not group:
        return False
    if text == "*":
        return True
    if text.endswith(".*"):
        prefix = text[:-2]
        return bool(prefix) and (group == prefix or group.startswith(prefix + "."))
    return group == text
