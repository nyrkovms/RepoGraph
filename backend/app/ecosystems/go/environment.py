"""Go environment for resolve (SPEC-MULTI-ECOSYSTEM.md §7.3, §9.5): the Go version and the root's `replace`/`exclude`.

`replace` and `exclude` count only in the main module (Go ignores them in dependencies), so they come with the request:
`replace` as `{"old": "path[@version]", "new": "path@version"}` (a module replacement; a directory replacement cannot be
resolved from a proxy), `exclude` as `"path@version"`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.ecosystems.base import InvalidVersion, NormalizedDependency
from app.ecosystems.go.versions import GoVersionScheme

DEFAULT_GO = "1.23"
_SCHEME = GoVersionScheme()


@dataclass(frozen=True, slots=True)
class GoEnvironment:
    go_version: str = DEFAULT_GO
    replace: tuple[tuple[str, str | None, str, str], ...] = ()   # (old path, old version or None, new path, new version)
    exclude: tuple[tuple[str, str], ...] = ()
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)

    def replacement(self, path: str, version: str) -> tuple[str, str] | None:
        """The module whose go.mod stands for `path@version`: an exact replacement wins over a path-wide one."""
        exact = next(((p, v) for old, ov, p, v in self.replace if old == path and ov == version), None)
        return exact or next(((p, v) for old, ov, p, v in self.replace if old == path and ov is None), None)


def _module_version(text: str, what: str) -> tuple[str, str]:
    path, at, version = text.strip().rpartition("@")
    if not at or not path:
        raise ValueError(f"{what} {text!r} is not path@version")
    try:
        _SCHEME.validate(version)
    except InvalidVersion as exc:
        raise ValueError(f"{what} {text!r}: {exc}") from None
    return path, version


def _replacements(raw: object) -> tuple[tuple[str, str | None, str, str], ...]:
    out: list[tuple[str, str | None, str, str]] = []
    for item in raw if isinstance(raw, Sequence) and not isinstance(raw, str) else ():
        if not isinstance(item, Mapping) or not isinstance(item.get("old"), str) or not isinstance(item.get("new"), str):
            raise ValueError(f"replace {item!r}: expected {{\"old\": \"path[@version]\", \"new\": \"path@version\"}}")
        old = str(item["old"])
        old_path, old_version = _module_version(old, "replace") if "@" in old else (old.strip(), None)
        new_path, new_version = _module_version(str(item["new"]), "replace")
        out.append((old_path, old_version, new_path, new_version))
    return tuple(out)


def parse_environment(raw: Mapping[str, object]) -> GoEnvironment:
    """`go_version` (default 1.23, the version the reference cases use), `replace`, `exclude`; ValueError if malformed."""
    values = {key: value for key, value in raw.items() if value not in (None, "", [], ())}
    if not values:
        return GoEnvironment(specified=False)
    excludes = values.get("exclude") or ()
    exclude_list = excludes if isinstance(excludes, Sequence) and not isinstance(excludes, str) else (excludes,)
    return GoEnvironment(
        go_version=str(values.get("go_version") or DEFAULT_GO).removeprefix("go"),
        replace=_replacements(values.get("replace")),
        exclude=tuple(_module_version(str(item), "exclude") for item in exclude_list),
    )


def dependency_applies(dep: NormalizedDependency, env: GoEnvironment, include_optional: bool) -> bool:
    """For the tree: every requirement of a go.mod applies; `// indirect` ones too (they are requirements of the build)."""
    return dep.dependency_type == "runtime"
