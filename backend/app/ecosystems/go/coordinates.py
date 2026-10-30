"""Go module paths and purls: the path as written, no normalization (SPEC-MULTI-ECOSYSTEM.md §4).

Case matters (`github.com/BurntSushi/toml`) and the major-version suffix is part of the identity
(`github.com/redis/go-redis/v9` is not `github.com/redis/go-redis`). `validate_path` follows
golang.org/x/mod/module.CheckPath; `escape_path`/`unescape_path` follow module.EscapePath/UnescapePath, the
encoding of module paths and versions in GOPROXY URLs.
"""

import re

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse

# The first element must be a lowercase domain-like name so that two paths never differ only in case on the host.
_FIRST_ELEMENT = re.compile(r"[a-z0-9.-]+")
_ELEMENT = re.compile(r"[A-Za-z0-9._~-]+")
# `/v2`, `/v10`; anything else that merely looks like a version (`/v2.1`, `/v2x`) is an ordinary element.
_MAJOR_ELEMENT = re.compile(r"v([0-9]+)")
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                               *(f"lpt{i}" for i in range(1, 10))})


def validate_path(path: str) -> None:
    if not path:
        raise ValueError("empty module path")
    if not path.isascii():
        raise ValueError(f"module path must be ASCII: {path!r}")
    if path.endswith("/"):
        raise ValueError(f"module path ends with a slash: {path!r}")

    elements = path.split("/")
    first = elements[0]
    if "." not in first:
        raise ValueError(f"missing dot in the first path element: {path!r}")
    if not _FIRST_ELEMENT.fullmatch(first):
        raise ValueError(f"the first path element is a lowercase domain name (a-z, 0-9, '-', '.'): {path!r}")
    if first.startswith("-"):
        raise ValueError(f"the first path element starts with '-': {path!r}")

    for element in elements:
        _check_element(element, path)

    last = elements[-1]
    if elements[0] == "gopkg.in":
        if not _gopkg_ok(last):
            raise ValueError(f"gopkg.in paths end in .vN: {path!r}")
        return
    major = _MAJOR_ELEMENT.fullmatch(last)
    if major:
        digits = major.group(1)
        if digits.startswith("0") or digits == "1":
            raise ValueError(f"major version suffix must be /v2 or higher, without leading zeros: {path!r}")


def _gopkg_ok(element: str) -> bool:
    """gopkg.in keeps the major version in the last element: `yaml.v3`, `yaml.v3-unstable`; `.v0` is allowed as in CheckPath."""
    stem = element.removesuffix("-unstable")
    head, dot, digits = stem.rpartition(".v")
    if not dot or not head or not digits.isdigit() or not digits.isascii():
        return False
    return digits == "0" or not digits.startswith("0")


def _check_element(element: str, path: str) -> None:
    if not element:
        raise ValueError(f"empty path element: {path!r}")
    if not _ELEMENT.fullmatch(element):
        raise ValueError(f"path element {element!r} has characters outside letters, digits and '-._~': {path!r}")
    if element in (".", "..") or element.startswith(".") or element.endswith("."):
        raise ValueError(f"path element {element!r} starts or ends with a dot: {path!r}")
    if element.partition(".")[0].lower() in _WINDOWS_RESERVED:
        raise ValueError(f"path element {element!r} is a reserved name on Windows: {path!r}")


def escape_path(path: str) -> str:
    """`github.com/BurntSushi/toml` -> `github.com/!burnt!sushi/toml`: proxies and caches may be case-insensitive."""
    return _escape(path)


def escape_version(version: str) -> str:
    return _escape(version)


def _escape(value: str) -> str:
    if not value.isascii():
        raise ValueError(f"cannot escape a non-ASCII string: {value!r}")
    return "".join(f"!{c.lower()}" if "A" <= c <= "Z" else c for c in value)


def unescape_path(escaped: str) -> str:
    out: list[str] = []
    bang = False
    for c in escaped:
        if bang:
            if not "a" <= c <= "z":
                raise ValueError(f"'!' must be followed by a lowercase letter in {escaped!r}")
            out.append(c.upper())
            bang = False
        elif c == "!":
            bang = True
        elif "A" <= c <= "Z":
            raise ValueError(f"uppercase letter in an escaped path: {escaped!r}")
        else:
            out.append(c)
    if bang:
        raise ValueError(f"escaped path ends with '!': {escaped!r}")
    return "".join(out)


class GoCoordinates:
    def parse(self, raw: str) -> PackageRef:
        path = raw.strip()
        validate_path(path)
        return PackageRef(ecosystem="go", namespace="", name=path)

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        parsed = parse(purl)
        if parsed.type != "golang":
            raise ValueError(f"not a golang purl: {purl}")
        if not parsed.namespace:
            raise ValueError(f"golang purls have a namespace (the module path without its last element): {purl}")
        return self.parse(f"{parsed.namespace}/{parsed.name}"), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        namespace, slash, name = ref.name.rpartition("/")
        if not slash:
            raise ValueError(f"a module path of one element has no purl namespace: {ref.name!r}")
        return Purl(type="golang", namespace=namespace, name=name, version=version).to_string()

    def display(self, ref: PackageRef) -> str:
        return ref.name
