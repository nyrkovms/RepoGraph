"""npm names and purls: `@scope/name` is namespace `scope` + name, no normalization (SPEC-MULTI-ECOSYSTEM.md §4)."""

import re

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse

MAX_NAME_LENGTH = 214

# What encodeURIComponent leaves alone, as validate-npm-package-name requires. Capitals are refused only for new
# packages (`validForOldPackages` allows them): the registry holds `JSONStream`, and the case is part of the name.
_SAFE = re.compile(r"[A-Za-z0-9._~!*'()-]+")
# Names the registry refuses on publish. Core-module names (`http`, `util`) are not refused here: such packages
# exist in the registry and a catalog has to be able to hold them.
_RESERVED = frozenset({"node_modules", "favicon.ico"})


def split_name(raw: str) -> tuple[str, str]:
    """(scope without `@`, name) of `@scope/name` or `name`, checking only that the shape is one of the two.

    Dependency targets go through here, not through `NpmCoordinates.parse`: the registry holds old packages that
    break the rules for new ones (`JSONStream`), and a dependency on one must not fail the package that declares it.
    """
    if raw.startswith("@"):
        scope, slash, name = raw[1:].partition("/")
        if not scope or not slash or not name or "/" in name:
            raise ValueError(f"not an npm scoped package name: {raw!r}")
        return scope, name
    if not raw or "/" in raw:
        raise ValueError(f"not an npm package name: {raw!r}")
    return "", raw


def validate_name(raw: str) -> None:
    """The rules of validate-npm-package-name that hold for old packages too (`validForOldPackages`), and the length."""
    if len(raw) > MAX_NAME_LENGTH:
        raise ValueError(f"npm package name is longer than {MAX_NAME_LENGTH} characters")
    scope, name = split_name(raw)
    if not scope and name.startswith((".", "_")):
        raise ValueError(f"npm package name cannot start with '.' or '_': {raw!r}")
    if name in _RESERVED:
        raise ValueError(f"not an allowed npm package name: {raw!r}")
    for part in (scope, name) if scope else (name,):
        if not _SAFE.fullmatch(part):
            raise ValueError(f"npm package name must be URL-safe: {raw!r}")


class NpmCoordinates:
    def parse(self, raw: str) -> PackageRef:
        name = raw.strip()
        validate_name(name)
        scope, local = split_name(name)
        return PackageRef(ecosystem="npm", namespace=scope, name=local)

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        parsed = parse(purl)
        if parsed.type != "npm":
            raise ValueError(f"not an npm purl: {purl}")
        if not parsed.namespace:
            return self.parse(parsed.name), parsed.version
        # The purl spec makes the scope the namespace, written with its `@` (`%40` when encoded).
        if not parsed.namespace.startswith("@") or "/" in parsed.namespace:
            raise ValueError(f"npm purl namespace must be one scope starting with '@': {purl}")
        return self.parse(f"{parsed.namespace}/{parsed.name}"), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        namespace = f"@{ref.namespace}" if ref.namespace else None
        return Purl(type="npm", namespace=namespace, name=ref.name, version=version).to_string()

    def display(self, ref: PackageRef) -> str:
        return f"@{ref.namespace}/{ref.name}" if ref.namespace else ref.name
