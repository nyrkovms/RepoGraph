"""PEP 440 version scheme (SPEC-MULTI-ECOSYSTEM.md §5.5).

The byte key is ported from design/schema-check/sortkey_proto.py (checked against `packaging` pairwise).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion as _PackagingInvalidVersion
from packaging.version import Version

from app.ecosystems.base import Bump, Constraint, InvalidVersion
from app.ecosystems.versions_common import encode_number, fallback_sort_key

_PRE_RANK = {"a": 1, "b": 2, "rc": 3}


@lru_cache(maxsize=65536)
def _parse(raw: str) -> Version:
    # Cached: level-1 linking and resolve test the same versions against many constraints.
    try:
        return Version(raw)
    except _PackagingInvalidVersion as exc:
        raise InvalidVersion(raw) from exc


def _key(version: Version) -> bytes:
    release = list(version.release)
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    out = encode_number(str(version.epoch))
    for part in release:
        out += b"\x02" + encode_number(str(part))
    out += b"\x01"
    if version.pre is None and version.post is None and version.dev is not None:
        out += b"\x00"  # 1.0.dev1 < 1.0a1
    elif version.pre is None:
        out += b"\x02"
    else:
        out += b"\x01" + bytes([_PRE_RANK[version.pre[0]]]) + encode_number(str(version.pre[1]))
    out += b"\x00" if version.post is None else b"\x01" + encode_number(str(version.post))
    out += b"\x02" if version.dev is None else b"\x01" + encode_number(str(version.dev))
    if version.local is None:
        out += b"\x00"
    else:
        out += b"\x01"
        for segment in version.local.split("."):
            out += (b"\x02" + encode_number(segment)) if segment.isdigit() else (b"\x01" + segment.lower().encode() + b"\x00")
        out += b"\x00"
    return out


@dataclass(frozen=True, slots=True)
class PyPIConstraint:
    _raw: str
    _spec: SpecifierSet | None  # None = any version

    def contains(self, version: str) -> bool:
        try:
            parsed = _parse(version)
        except InvalidVersion:
            return False
        if self._spec is None:
            return True
        return self._spec.contains(parsed, prereleases=True)

    def raw(self) -> str:
        return self._raw


class PyPIVersionScheme:
    def validate(self, raw: str) -> None:
        # A version whose number does not fit the key encoding cannot be stored sortably: treat it as invalid.
        try:
            _key(_parse(raw))
        except ValueError as exc:
            if isinstance(exc, InvalidVersion):
                raise
            raise InvalidVersion(raw) from exc

    def sort_key(self, raw: str) -> bytes:
        try:
            return _key(_parse(raw))
        except ValueError:
            return fallback_sort_key(raw)

    def is_prerelease(self, raw: str) -> bool:
        try:
            version = Version(raw)
        except _PackagingInvalidVersion:
            return False
        return version.is_prerelease or version.is_devrelease

    def bump(self, previous: str | None, current: str) -> Bump | None:
        # Same logic as fetcher.events.classify_bump; that function switches to this one in B1.
        if previous is None:
            return None
        try:
            prev_ver = Version(previous)
            cur_ver = Version(current)
        except _PackagingInvalidVersion:
            return "other"

        if cur_ver.is_prerelease or cur_ver.is_devrelease:
            return "pre"
        if cur_ver <= prev_ver:
            return "other"

        prev_release = prev_ver.release + (0, 0, 0)
        cur_release = cur_ver.release + (0, 0, 0)
        if cur_ver.epoch != prev_ver.epoch or cur_release[0] != prev_release[0]:
            return "major"
        if cur_release[1] != prev_release[1]:
            return "minor"
        if cur_release == prev_release:
            # Same release segment, only local/post label differs (e.g. 2.8.0 -> 2.8.0+cpu).
            return "other"
        return "patch"

    def constraint(self, raw: str) -> Constraint:
        text = raw.strip()
        if text in ("", "*"):
            return PyPIConstraint(raw, None)
        try:
            return PyPIConstraint(raw, SpecifierSet(text))
        except InvalidSpecifier as exc:
            raise InvalidVersion(f"invalid PEP 440 specifier: {raw!r}") from exc
