"""crates.io names and purls: the name as published, no normalization (SPEC-MULTI-ECOSYSTEM.md §4, Q21).

crates.io refuses a name that differs from an existing one only in case or `-`/`_`, so the published spelling is a
key on its own; the sparse index serves the crate under its lowercased name and nothing else.
"""

import re

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse

MAX_NAME_LENGTH = 64

# What crates.io accepts on publish: ASCII letters, digits, `-` and `_`, starting with a letter.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Dependency targets: the index holds what crates.io accepted over the years; only the shape is checked.
_DEPENDENCY_NAME = re.compile(r"[A-Za-z0-9_-]+")


def validate_name(raw: str) -> None:
    if len(raw) > MAX_NAME_LENGTH:
        raise ValueError(f"crate name is longer than {MAX_NAME_LENGTH} characters: {raw!r}")
    if not _NAME.fullmatch(raw):
        raise ValueError(f"not a crate name (ASCII letters, digits, '-' and '_', a letter first): {raw!r}")


def dependency_ref(raw: str) -> PackageRef:
    """The target of a dependency as the index names it; raises ValueError only for what cannot be a name at all."""
    if not _DEPENDENCY_NAME.fullmatch(raw):
        raise ValueError(f"not a crate name: {raw!r}")
    return PackageRef(ecosystem="crates", namespace="", name=raw)


def index_path(name: str) -> str:
    """The file of a crate in a sparse index: `1/a`, `2/ab`, `3/a/abc`, `se/rd/serde`, all lowercase."""
    lower = name.lower()
    if len(lower) <= 2:
        return f"{len(lower)}/{lower}"
    if len(lower) == 3:
        return f"3/{lower[0]}/{lower}"
    return f"{lower[:2]}/{lower[2:4]}/{lower}"


class CratesCoordinates:
    def parse(self, raw: str) -> PackageRef:
        name = raw.strip()
        validate_name(name)
        return PackageRef(ecosystem="crates", namespace="", name=name)

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        parsed = parse(purl)
        if parsed.type != "cargo":
            raise ValueError(f"not a cargo purl: {purl}")
        if parsed.namespace:
            raise ValueError(f"cargo purls have no namespace: {purl}")
        return self.parse(parsed.name), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        return Purl(type="cargo", namespace=None, name=ref.name, version=version).to_string()

    def display(self, ref: PackageRef) -> str:
        return ref.name
