"""RPM versions (EVR) and capability constraints as rpm 4.16 reads them (SPEC-MULTI-ECOSYSTEM.md §5.5, §9.8).

A version string is `[epoch:]version[-release]`, split like `rpmEVRparse`: the epoch is the digits before the first
`:` (only when they come first), the release is what follows the *last* `-`, so a version never holds a dash. A missing
epoch is 0; a missing release is smaller than any release, an empty one included (`rpm.labelCompare` with a `None`
release). Order is `rpmvercmp` on the version, then on the release, after the epoch as a number: both strings are cut
into runs of digits and runs of ASCII letters, every other character only separates (and is skipped, so `1.0` equals
`1_0` and `1.0.`); a digit run beats a letter run, digit runs compare as numbers (leading zeros ignored, any length),
letter runs as bytes (`B` < `a`, `a` < `ab`); the string with segments left wins. `~` sorts before everything, even
the end of the string (`1.0~rc1 < 1.0`), and `^` after the end but before any segment (`1.0 < 1.0^git1 < 1.0.1`), as
in rpm >= 4.15. Non-ASCII characters are separators, as in rpm (it works on bytes).

`validate` rejects what no package can hold: the empty string, whitespace and control characters, an empty version
(`-1`, `1:`) and an epoch that is not a number (`a:1.0`, `:1.0`, `1.5:1.0`; rpm would read the text as a version).
Everything else is a version, as for rpm: an empty release (`1.0-`) and a second `:` or `-` (`1:2:3-1` is epoch 1,
version `2:3`; `1.0-1-2` is version `1.0-1`, release `2`).

RPM has no prerelease notion apart from the `~` convention: `is_prerelease` is true when the *version* holds a `~`
(`1.0~rc1-1`); a `~` in the release (`1-0.1~rc1`) is a snapshot or bootstrap marker, not a prerelease of the version.

A constraint is a capability flag with an EVR as written in specs (`>= 1.2-3`, `=` / `==`, `<`, `<=` / `=<`, `>`,
`>=` / `=>`) or in repodata (`GE 1.2-3`, `EQ`, `LT`, `LE`, `GT`), or empty / `*` for "any version". A candidate matches
as in `rpmdsCompare` of the requirement with a provide `= candidate`: the epochs, then the versions, then the releases
only when *both* sides have one (a constraint without a release ignores the candidate's, `>= 1.2` holds for `1.2-0.1`);
when the versions are equal and the candidate (the provide) has no release it matches whatever the operator and the
release asked for (`> 1.0-1` holds for a bare `1.0`). The vectors in tests/fixtures/rpm/versions are written by
scripts/generate_rpm_version_vectors.sh from rpm itself on rockylinux:9.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from app.ecosystems.base import Bump, InvalidVersion
from app.ecosystems.versions_common import encode_number, fallback_sort_key

_SEGMENT = re.compile(r"~|\^|[0-9]+|[A-Za-z]+")   # ASCII only: rpm's risalnum ignores the locale and non-ASCII bytes


@dataclass(frozen=True, slots=True)
class Evr:
    epoch: str                                     # digits without leading zeros ("0" when missing)
    version: str
    release: str | None                            # None: no dash at all; "" for `1.0-`


def _cmp(a: Any, b: Any) -> int:
    return int(a > b) - int(a < b)


def _cmp_digits(a: str, b: str) -> int:
    a, b = a.lstrip("0"), b.lstrip("0")
    return _cmp((len(a), a), (len(b), b))


@lru_cache(maxsize=65536)
def parse(raw: str) -> Evr:
    """Split an EVR like `rpmEVRparse`; raises InvalidVersion for what `validate` rejects."""
    if not raw or any(c.isspace() or ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in raw):
        raise InvalidVersion(f"{raw!r}: an RPM version is not empty and has no whitespace or control characters")
    epoch, rest = "0", raw
    colon = raw.find(":")
    if colon >= 0 and "-" not in raw[:colon]:
        digits = raw[:colon]
        if not (digits.isascii() and digits.isdigit()):
            raise InvalidVersion(f"{raw!r}: the epoch is not a number")
        epoch, rest = digits.lstrip("0") or "0", raw[colon + 1:]
    head, dash, tail = rest.rpartition("-")
    version = head if dash else rest
    if not version:
        raise InvalidVersion(f"{raw!r}: the version is empty")
    return Evr(epoch, version, tail if dash else None)


def _valid(raw: str) -> bool:
    try:
        parse(raw)
    except InvalidVersion:
        return False
    return True


def vercmp(a: str, b: str) -> int:
    """`rpmvercmp` as -1, 0 or 1 (rpm >= 4.15, with `~` and `^`)."""
    if a == b:
        return 0
    left, right = _SEGMENT.findall(a), _SEGMENT.findall(b)
    for i in range(max(len(left), len(right))):
        x = left[i] if i < len(left) else None
        y = right[i] if i < len(right) else None
        if x == "~" or y == "~":
            if x != "~":
                return 1
            if y != "~":
                return -1
            continue
        if x == "^" or y == "^":
            if x is None:
                return -1
            if y is None:
                return 1
            if x != "^":
                return 1
            if y != "^":
                return -1
            continue
        if x is None:
            return -1
        if y is None:
            return 1
        x_number, y_number = x[0].isdigit(), y[0].isdigit()
        if x_number != y_number:
            return 1 if x_number else -1           # a digit run is newer than a letter run
        order = _cmp_digits(x, y) if x_number else _cmp(x, y)
        if order:
            return order
    return 0


def _compare_parsed(a: Evr, b: Evr) -> int:
    order = _cmp_digits(a.epoch, b.epoch) or vercmp(a.version, b.version)
    if order:
        return order
    if a.release is None or b.release is None:
        return _cmp(a.release is not None, b.release is not None)   # `rpm.labelCompare`: no release is below any
    return vercmp(a.release, b.release)


def compare(a: str, b: str) -> int:
    """`rpm.labelCompare` on two EVR strings as -1, 0 or 1; raises InvalidVersion for a string `validate` rejects."""
    return _compare_parsed(parse(a), parse(b))


# --- sort key ----------------------------------------------------------------------------------------------


# Tags of a segment in key order: `~` is below the end of the string, `^` above it and below any segment.
_TILDE, _END, _CARET, _ALPHA, _NUMBER = (bytes([i]) for i in range(5))


def _encode_digits(digits: str) -> bytes:
    digits = digits.lstrip("0") or "0"
    if len(digits) < 255:
        return encode_number(digits)
    return b"\xff" + len(digits).to_bytes(4, "big") + digits.encode()   # above every length-prefixed number


def _part_key(text: str) -> bytes:
    out = b""
    for segment in _SEGMENT.findall(text):
        if segment == "~":
            out += _TILDE
        elif segment == "^":
            out += _CARET
        elif segment[0].isdigit():
            out += _NUMBER + _encode_digits(segment)
        else:
            out += _ALPHA + segment.encode() + b"\x00"
    return out + _END


def _evr_key(evr: Evr) -> bytes:
    key = _encode_digits(evr.epoch) + _part_key(evr.version)
    return key if evr.release is None else key + _part_key(evr.release)   # a missing release is a prefix: the smallest


# --- bump --------------------------------------------------------------------------------------------------


def _numbers(version: str) -> tuple[str, str, str]:
    """The leading numeric segments of a version padded with zeros to three: `3.0.7` -> 3, 0, 7; `1.1.1k` -> 1, 1, 1."""
    found: list[str] = []
    for segment in _SEGMENT.findall(version):
        if not segment[0].isdigit() or len(found) == 3:
            break
        found.append(segment.lstrip("0") or "0")
    found += ["0"] * (3 - len(found))
    return found[0], found[1], found[2]


# --- constraints -------------------------------------------------------------------------------------------


Flag = Literal["EQ", "LT", "LE", "GT", "GE"]

_FLAGS: dict[str, Flag] = {
    "=": "EQ", "==": "EQ", "EQ": "EQ", "<": "LT", "LT": "LT", "<=": "LE", "=<": "LE", "LE": "LE",
    ">": "GT", "GT": "GT", ">=": "GE", "=>": "GE", "GE": "GE",
}
_CONSTRAINT = re.compile(r"(?:(<=|>=|==|=<|=>|=|<|>)\s*|(EQ|LT|LE|GT|GE)\s+)(?![<>=])(\S+)")


def _matches(flag: Flag, wanted: Evr, candidate: Evr) -> bool:
    """`rpmdsCompare` of a requirement `flag wanted` against a provide `= candidate`."""
    order = _cmp_digits(candidate.epoch, wanted.epoch) or vercmp(candidate.version, wanted.version)
    if order == 0 and wanted.release:
        if not candidate.release:
            return True                            # a provide without a release stands for every release
        order = vercmp(candidate.release, wanted.release)
    if order < 0:
        return flag in ("LT", "LE")
    if order > 0:
        return flag in ("GT", "GE")
    return flag in ("EQ", "LE", "GE")


@dataclass(frozen=True, slots=True)
class RpmConstraint:
    _raw: str
    flag: Flag | None                              # None: any version
    evr: Evr | None

    def raw(self) -> str:
        return self._raw

    def contains(self, version: str) -> bool:
        """True when a package `version` satisfies the capability; a string that is no EVR satisfies nothing."""
        if not _valid(version):
            return False
        if self.flag is None or self.evr is None:
            return True
        return _matches(self.flag, self.evr, parse(version))

    def best_match(self, versions: list[str]) -> str | None:
        """The highest satisfying version (the first one among equal versions); strings that are no EVR are skipped."""
        best: str | None = None
        for candidate in versions:
            if self.contains(candidate) and (best is None or compare(candidate, best) > 0):
                best = candidate
        return best


@lru_cache(maxsize=16384)
def parse_constraint(raw: str) -> RpmConstraint:
    text = raw.strip()
    if text in ("", "*"):
        return RpmConstraint(raw, None, None)
    match = _CONSTRAINT.fullmatch(text)
    if not match:
        raise InvalidVersion(f"{raw!r}: an RPM constraint is `<flag> <epoch:version-release>` or empty")
    return RpmConstraint(raw, _FLAGS[match[1] or match[2]], parse(match[3]))


class RpmVersionScheme:
    def validate(self, raw: str) -> None:
        parse(raw)

    def sort_key(self, raw: str) -> bytes:
        try:
            return _evr_key(parse(raw))
        except InvalidVersion:
            return fallback_sort_key(raw)

    def is_prerelease(self, raw: str) -> bool:
        return _valid(raw) and "~" in parse(raw).version

    def bump(self, previous: str | None, current: str) -> Bump | None:
        if previous is None:
            return None
        if not (_valid(previous) and _valid(current)):
            return "other"
        before, after = parse(previous), parse(current)
        if "~" in after.version:
            return "pre"
        if _compare_parsed(after, before) <= 0 or before.epoch != after.epoch:
            return "other"                         # a downgrade, or a new epoch (the numbers do not compare)
        if vercmp(before.version, after.version) == 0:
            return "patch"                         # only a new release: a rebuild or a backported fix (SPEC §5.5)
        old, new = _numbers(before.version), _numbers(after.version)
        if new[0] != old[0]:
            return "major"
        if new[1] != old[1]:
            return "minor"
        return "patch"                             # the third number, a fourth one or letters

    def constraint(self, raw: str) -> RpmConstraint:
        return parse_constraint(raw)
