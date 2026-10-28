"""Package URL (purl) parsing and building, generic grammar only (SPEC-MULTI-ECOSYSTEM.md §4).

Name normalization is ecosystem-specific and lives in the plugins; this module also maps our
ecosystem names to purl types.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from urllib.parse import quote, unquote

from app.ecosystems.base import Ecosystem

SCHEME = "pkg:"

_TYPE_RE = re.compile(r"[a-z][a-z0-9.+-]*")
_QUALIFIER_KEY_RE = re.compile(r"[a-z][a-z0-9._-]*")

_PURL_TYPES: dict[str, str] = {
    "pypi": "pypi",
    "npm": "npm",
    "maven": "maven",
    "nuget": "nuget",
    "go": "golang",
    "crates": "cargo",
    "rpm": "rpm",
}
_ECOSYSTEMS: dict[str, Ecosystem] = {
    "pypi": "pypi",
    "npm": "npm",
    "maven": "maven",
    "nuget": "nuget",
    "golang": "go",
    "cargo": "crates",
    "rpm": "rpm",
}


def purl_type_for(ecosystem: str) -> str:
    try:
        return _PURL_TYPES[ecosystem]
    except KeyError:
        raise ValueError(f"unknown ecosystem: {ecosystem!r}") from None


def ecosystem_for(purl_type: str) -> Ecosystem:
    try:
        return _ECOSYSTEMS[purl_type.lower()]
    except KeyError:
        raise ValueError(f"no ecosystem for purl type: {purl_type!r}") from None


class Qualifiers(Mapping[str, str]):
    """Immutable, hashable qualifier mapping; iterates in key order."""

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, str] | None = None) -> None:
        self._items: tuple[tuple[str, str], ...] = tuple(sorted((items or {}).items()))

    def __getitem__(self, key: str) -> str:
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (k for k, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        return f"Qualifiers({dict(self._items)!r})"


@dataclass(frozen=True)
class Purl:
    """Components are stored decoded. The constructor canonicalizes them: type and qualifier
    keys are lowercased, qualifiers with empty values are dropped, empty optional parts become None."""

    type: str
    name: str
    namespace: str | None = None
    version: str | None = None
    qualifiers: Mapping[str, str] = Qualifiers()
    subpath: str | None = None

    def __post_init__(self) -> None:
        purl_type = self.type.lower()
        if not _TYPE_RE.fullmatch(purl_type):
            raise ValueError(f"invalid purl type: {self.type!r}")
        if not self.name:
            raise ValueError("purl name is required")

        qualifiers: dict[str, str] = {}
        for key, value in self.qualifiers.items():
            key = key.lower()
            if not _QUALIFIER_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid purl qualifier key: {key!r}")
            if key in qualifiers:
                raise ValueError(f"duplicate purl qualifier key: {key!r}")
            if value:
                qualifiers[key] = value

        object.__setattr__(self, "type", purl_type)
        object.__setattr__(self, "namespace", _join_segments(self.namespace))
        object.__setattr__(self, "version", self.version or None)
        object.__setattr__(self, "qualifiers", Qualifiers(qualifiers))
        object.__setattr__(self, "subpath", _clean_subpath(self.subpath))

    def to_string(self) -> str:
        out = [SCHEME, self.type, "/"]
        if self.namespace:
            out.extend(f"{_quote(segment)}/" for segment in self.namespace.split("/"))
        out.append(_quote(self.name))
        if self.version:
            out.append(f"@{_quote(self.version)}")
        if self.qualifiers:
            pairs = (f"{key}={_quote(value, safe=':/')}" for key, value in self.qualifiers.items())
            out.append(f"?{'&'.join(pairs)}")
        if self.subpath:
            out.append(f"#{'/'.join(_quote(segment) for segment in self.subpath.split('/'))}")
        return "".join(out)

    def __str__(self) -> str:
        return self.to_string()


def parse(purl: str) -> Purl:
    try:
        return _parse(purl)
    except ValueError as exc:
        raise ValueError(f"invalid purl {purl!r}: {exc}") from None


def _parse(purl: str) -> Purl:
    if purl[: len(SCHEME)].lower() != SCHEME:
        raise ValueError("must start with 'pkg:'")
    rest = purl[len(SCHEME) :].lstrip("/")

    rest, _, subpath = rest.partition("#")
    rest, _, qualifier_part = rest.partition("?")

    purl_type, slash, path = rest.partition("/")
    if not purl_type:
        raise ValueError("type is missing")
    if not slash:
        raise ValueError("name is missing")

    # An unencoded '@' before the last '/' belongs to an npm-style scope, not to the version.
    head, sep, tail = path.rpartition("/")
    name_part, at, version = tail.rpartition("@")
    if not at:
        name_part, version = tail, ""
    path = f"{head}{sep}{name_part}"

    segments = [_decode(segment) for segment in path.split("/") if segment]
    if not segments:
        raise ValueError("name is missing")

    return Purl(
        type=purl_type,
        namespace="/".join(segments[:-1]) or None,
        name=segments[-1],
        version=_decode(version),
        qualifiers=_parse_qualifiers(qualifier_part),
        subpath="/".join(_decode(s) for s in subpath.split("/") if s),
    )


def _parse_qualifiers(raw: str) -> dict[str, str]:
    qualifiers: dict[str, str] = {}
    for pair in raw.split("&"):
        if not pair:
            continue
        key, eq, value = pair.partition("=")
        if not eq:
            raise ValueError(f"qualifier {pair!r} has no '='")
        key = key.lower()
        if key in qualifiers:
            raise ValueError(f"duplicate qualifier key {key!r}")
        qualifiers[key] = _decode(value)
    return qualifiers


def _decode(value: str) -> str:
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        raise ValueError(f"bad percent-encoding in {value!r}") from None


def _quote(value: str, safe: str = ":") -> str:
    return quote(value, safe=safe)


def _join_segments(path: str | None) -> str | None:
    return "/".join(s for s in (path or "").split("/") if s) or None


def _clean_subpath(subpath: str | None) -> str | None:
    segments = [s for s in (subpath or "").split("/") if s not in ("", ".", "..")]
    return "/".join(segments) or None
