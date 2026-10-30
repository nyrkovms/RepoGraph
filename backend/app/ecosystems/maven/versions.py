"""Maven versions and version ranges as maven-artifact 3.9.x reads them (SPEC-MULTI-ECOSYSTEM.md §5.5, §9.7).

Order is `ComparableVersion.compareTo`: the lowercased string is cut at `.`, `-` and digit/letter transitions into a
tree of numbers, qualifier strings and nested lists (a `-`, or a qualifier after a number, opens a sublist); trailing
nulls (`0`, `""`, empty lists) are trimmed on every level. A number is an Int, Long or BigInteger item by its digit
count, and different kinds compare by kind first, which agrees with the numeric order except for a run of 19+ zeros.
Known qualifiers are `alpha < beta < milestone < rc (cr) < snapshot < "" (ga, final, release) < sp`; `a`, `b` and `m`
mean alpha, beta and milestone only right before a digit; an unknown qualifier sorts after all of them, compared as
a string (UTF-16 code units, like Java). An item missing from the shorter list compares as null: a number is
above it when not 0, a qualifier by its place against the release, a list by its first non-null item.

Every string is a valid Maven version: `ComparableVersion` accepts anything. `validate` rejects only the empty
string and strings holding whitespace or control characters, which a POM never publishes (and a repository path
cannot hold); the scheme still compares such strings the way Maven does, and the tests check that.

`sort_key` is a byte key that agrees with Maven on every pair of the vectors. Maven's order is not transitive when a
qualifier stands between dots after a number or a sublist starts with a qualifier next to a list: `1-alpha < 1 < 1.sp.5`
but `1.sp.5 < 1-alpha`, and `1.0.alpha.1 < 1 < 1-1 < 1.0.alpha.1`. No byte key (and no total order) can follow a
cycle. The key puts the end of a list between the negative and the non-negative items, which is right for the
everyday versions and for `1.0.alpha.1` against a plain release, and wrong only for such cycles (the pairs that close
them); `compare` is the exact port and is what ranges and `bump` use. The vectors in tests/fixtures/maven/versions are
written by scripts/generate_maven_version_vectors.sh from maven-artifact itself, the non-transitive ones apart.

A range is `VersionRange.createFromVersionSpec`: a bare version is a soft recommendation (any version is "contained"),
`[1.0]` is exact, `[1.0,2.0)`, `(,1.0]`, `[1.0,)`, and unions `[1,2),[3,4)` of intervals that do not overlap.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.ecosystems.base import Bump, InvalidVersion
from app.ecosystems.versions_common import encode_number, fallback_sort_key

MAX_INT_LENGTH = 9
MAX_LONG_LENGTH = 18

_INT, _LONG, _BIG = 0, 1, 2                        # number kinds in Java's compare order (Int < Long < BigInteger)

QUALIFIERS = ("alpha", "beta", "milestone", "rc", "snapshot", "", "sp")
_RELEASE = QUALIFIERS.index("")
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_SHORT = {"a": "alpha", "b": "beta", "m": "milestone"}   # only when followed by a digit


@dataclass(frozen=True, slots=True)
class Number:
    digits: str                                    # ASCII, no leading zeros ("0" for zero): any size, as BigInteger
    kind: int

    def __str__(self) -> str:
        return self.digits

    @property
    def order(self) -> tuple[int, int, str]:
        return self.kind, len(self.digits), self.digits


@dataclass(frozen=True, slots=True)
class Qualifier:
    value: str

    def __str__(self) -> str:
        return self.value

    @property
    def rank(self) -> int:
        """Index in QUALIFIERS, or len(QUALIFIERS) for an unknown qualifier."""
        return QUALIFIERS.index(self.value) if self.value in QUALIFIERS else len(QUALIFIERS)


@dataclass(slots=True)
class Sublist:
    items: list[Item]

    def __str__(self) -> str:
        out = ""
        for item in self.items:
            if out:
                out += "-" if isinstance(item, Sublist) else "."
            out += str(item)
        return out


Item = Number | Qualifier | Sublist


def _is_null(item: Item) -> bool:
    if isinstance(item, Number):
        return item.digits == "0"
    if isinstance(item, Qualifier):
        return item.value == ""
    return not item.items


def _qualifier(text: str, followed_by_digit: bool) -> Qualifier:
    if followed_by_digit and len(text) == 1:
        text = _SHORT.get(text, text)
    return Qualifier(_ALIASES.get(text, text))


def _number(digits: str) -> Number:
    # stripLeadingZeroes keeps an all-zero string as it is, so 19 zeros are a BigInteger (and sort above every Int)
    stripped = digits.lstrip("0") or digits
    kind = _INT if len(stripped) <= MAX_INT_LENGTH else _LONG if len(stripped) <= MAX_LONG_LENGTH else _BIG
    ascii_digits = "".join(str(unicodedata.decimal(c)) for c in digits).lstrip("0") or "0"
    return Number(ascii_digits, kind)


def _item(is_digit: bool, text: str) -> Item:
    return _number(text) if is_digit else _qualifier(text, False)


def _normalize(items: list[Item]) -> None:
    """Drop trailing nulls, looking through a trailing non-null sublist (`1.0-alpha` is `1-alpha`)."""
    for i in range(len(items) - 1, -1, -1):
        last = items[i]
        if _is_null(last):
            del items[i]
        elif not isinstance(last, Sublist):
            break


def _is_digit(char: str) -> bool:
    return char.isdecimal() and ord(char) <= 0xFFFF   # Java works on UTF-16 units: a surrogate is no digit


@lru_cache(maxsize=65536)
def _parse(raw: str) -> Sublist:
    version = raw.lower()
    root = Sublist([])
    current = root
    stack = [root]
    is_digit = False
    start = 0

    def descend() -> Sublist:
        nested = Sublist([])
        current.items.append(nested)
        stack.append(nested)
        return nested

    for i, char in enumerate(version):
        if char in ".-":
            current.items.append(Number("0", _INT) if i == start else _item(is_digit, version[start:i]))
            start = i + 1
            if char == "-":
                current = descend()
        elif _is_digit(char):
            if not is_digit and i > start:
                if current.items:                  # 1.0.0.X1 is read as 1.0.0-X1
                    current = descend()
                current.items.append(_qualifier(version[start:i], True))
                start = i
                current = descend()
            is_digit = True
        else:
            if is_digit and i > start:
                current.items.append(_number(version[start:i]))
                start = i
                current = descend()
            is_digit = False
    if len(version) > start:
        if not is_digit and current.items:
            current = descend()
        current.items.append(_item(is_digit, version[start:]))
    for nested in reversed(stack):
        _normalize(nested.items)
    return root


def canonical(raw: str) -> str:
    """`ComparableVersion.getCanonical()`: `1.0.0` -> `1`, `1.0-alpha1` -> `1-alpha-1`, `1.0.RELEASE` -> `1`."""
    return str(_parse(raw))


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be", "surrogatepass")


def _cmp(a: Any, b: Any) -> int:
    return int(a > b) - int(a < b)


def _compare_null(item: Item) -> int:
    """`item.compareTo(null)`: how an item compares with a missing one."""
    if isinstance(item, Number):
        return 0 if item.digits == "0" else 1
    if isinstance(item, Qualifier):
        return _cmp(item.rank, _RELEASE) if item.rank < len(QUALIFIERS) else 1
    for nested in item.items:                      # the first item that differs from null decides (MNG-6964)
        result = _compare_null(nested)
        if result:
            return result
    return 0


def _compare_items(a: Item, b: Item | None) -> int:
    if b is None:
        return _compare_null(a)
    if isinstance(a, Number):
        if isinstance(b, Number):
            return _cmp(a.order, b.order)
        return 1                                   # 1.1 > 1-sp, 1.1 > 1-1
    if isinstance(a, Qualifier):
        if isinstance(b, Qualifier):
            return _cmp(_utf16(_comparable_qualifier(a)), _utf16(_comparable_qualifier(b)))
        return -1                                  # 1.any < 1.1, 1.any < 1-1
    if isinstance(b, Number):
        return -1                                  # 1-1 < 1.0.x
    if isinstance(b, Qualifier):
        return 1                                   # 1-1 > 1-sp
    return _compare_lists(a, b)


def _comparable_qualifier(qualifier: Qualifier) -> str:
    """`StringItem.comparableQualifier`: the digit of a known qualifier, `7-name` for an unknown one."""
    return str(qualifier.rank) if qualifier.rank < len(QUALIFIERS) else f"{len(QUALIFIERS)}-{qualifier.value}"


def _compare_lists(a: Sublist, b: Sublist) -> int:
    for i in range(max(len(a.items), len(b.items))):
        left = a.items[i] if i < len(a.items) else None
        right = b.items[i] if i < len(b.items) else None
        if left is None:
            assert right is not None
            result = -_compare_items(right, None)
        else:
            result = _compare_items(left, right)
        if result:
            return result
    return 0


def compare(a: str, b: str) -> int:
    """`ComparableVersion.compareTo` as -1, 0 or 1; exact for every pair, transitive or not."""
    return _compare_lists(_parse(a), _parse(b))


# --- sort key ----------------------------------------------------------------------------------------------


# Tags of an item in a list, in key order. A list ends with _END; an item below _END is smaller than a missing one
# (`1-alpha < 1`), one above it is bigger. A null item (`0`, `""`) in the middle takes the side of what follows it.
_NEG_QUALIFIER, _NEG_LIST, _NULL_QUALIFIER_NEG_TAIL, _ZERO_NEG_TAIL, _END, _QUALIFIER, _LIST, _NUMBER = (
    bytes([i]) for i in range(1, 9))


def _encode_digits(digits: str) -> bytes:
    if len(digits) < 255:
        return encode_number(digits)
    return b"\xff" + len(digits).to_bytes(4, "big") + digits.encode()   # above every length-prefixed number


def _compare_null_all(items: list[Item]) -> int:
    for item in items:
        result = _compare_null(item)
        if result:
            return result
    return 0


def _key_items(items: list[Item]) -> bytes:
    out = b""
    for index, item in enumerate(items):
        negative_tail = _is_null(item) and _compare_null_all(items[index + 1:]) < 0
        if isinstance(item, Number):
            out += (_ZERO_NEG_TAIL if negative_tail else _NUMBER) + bytes([item.kind]) + _encode_digits(item.digits)
        elif isinstance(item, Qualifier):
            rank = item.rank
            body = bytes([rank]) + (_utf16(item.value) + b"\x00\x00" if rank == len(QUALIFIERS) else b"")
            out += (_NEG_QUALIFIER if rank < _RELEASE else _NULL_QUALIFIER_NEG_TAIL if negative_tail else _QUALIFIER) + body
        else:
            out += (_NEG_LIST if _compare_null(item) < 0 else _LIST) + _key_items(item.items)
    return out + _END


def _valid(raw: str) -> bool:
    return bool(raw) and not any(c.isspace() or unicodedata.category(c) == "Cc" for c in raw)


def _prerelease(items: list[Item]) -> bool:
    return any(
        (item.rank < _RELEASE) if isinstance(item, Qualifier) else isinstance(item, Sublist) and _prerelease(item.items)
        for item in items)


def _numbers(version: Sublist) -> tuple[str, str, str]:
    """The leading numeric items, padded with zeros to three: what `1.2.3-SP1` or `1.2` mean as major/minor/patch."""
    found: list[str] = []
    for item in version.items:
        if not isinstance(item, Number) or len(found) == 3:
            break
        found.append(item.digits)
    found += ["0"] * (3 - len(found))
    return found[0], found[1], found[2]


# --- ranges ------------------------------------------------------------------------------------------------


Restriction = tuple[str | None, bool, str | None, bool]   # lower, lower_inclusive, upper, upper_inclusive


def _contains(restriction: Restriction, version: str) -> bool:
    """`Restriction.containsVersion`."""
    lower, lower_inclusive, upper, upper_inclusive = restriction
    if lower is not None:
        order = compare(lower, version)
        if order > 0 or (order == 0 and not lower_inclusive):
            return False
    if upper is not None:
        order = compare(upper, version)
        if order < 0 or (order == 0 and not upper_inclusive):
            return False
    return True


@dataclass(frozen=True, slots=True)
class MavenConstraint:
    _raw: str
    recommended: str | None                        # the version of a bare spec (a soft recommendation), else None
    restrictions: tuple[Restriction, ...]          # (None, False, None, False) for a bare version

    def raw(self) -> str:
        return self._raw

    def contains(self, version: str) -> bool:
        """`VersionRange.containsVersion`: any interval holds it; a bare version holds every version."""
        return any(_contains(r, version) for r in self.restrictions)

    def best_match(self, versions: list[str]) -> str | None:
        """`VersionRange.matchVersion`: the highest contained version, the first one among equal versions.

        Strings that are not valid versions are skipped.
        """
        best: str | None = None
        for candidate in versions:
            if _valid(candidate) and self.contains(candidate) and (best is None or compare(candidate, best) > 0):
                best = candidate
        return best


def _parse_restriction(spec: str) -> Restriction:
    lower_inclusive, upper_inclusive = spec.startswith("["), spec.endswith("]")
    body = spec[1:-1].strip()
    comma = body.find(",")
    if comma < 0:
        if not (lower_inclusive and upper_inclusive):
            raise InvalidVersion(f"Single version must be surrounded by []: {spec}")
        return body, True, body, True
    lower, upper = body[:comma].strip(), body[comma + 1:].strip()
    if lower and upper:
        order = compare(upper, lower)
        if order < 0 or (order == 0 and not (lower_inclusive and upper_inclusive)):
            raise InvalidVersion(f"Range defies version ordering: {spec}")
    return lower or None, lower_inclusive, upper or None, upper_inclusive


@lru_cache(maxsize=16384)
def parse_range(raw: str) -> MavenConstraint:
    """`VersionRange.createFromVersionSpec`; raises InvalidVersion where Maven throws InvalidVersionSpecification.

    Like Maven, it accepts the empty spec (no interval, so nothing is contained) and a bare version with anything in
    it; the checks are the interval ones: unclosed, a single version without `[]`, reversed bounds, overlapping
    intervals and text after intervals. An interval after one with no upper bound is not checked for overlap.
    """
    restrictions: list[Restriction] = []
    rest = raw
    upper: str | None = None
    while rest.startswith(("[", "(")):
        close, square = rest.find(")"), rest.find("]")
        end = square
        if (square < 0 or close < square) and close >= 0:
            end = close
        if end < 0:
            raise InvalidVersion(f"Unbounded range: {raw}")
        restriction = _parse_restriction(rest[:end + 1])
        if upper is not None and (restriction[0] is None or compare(restriction[0], upper) < 0):
            raise InvalidVersion(f"Ranges overlap: {raw}")
        restrictions.append(restriction)
        upper = restriction[2]
        rest = rest[end + 1:].strip()
        if rest.startswith(","):
            rest = rest[1:].strip()
    recommended: str | None = None
    if rest:
        if restrictions:
            raise InvalidVersion(f"Only fully-qualified sets allowed in multiple set scenario: {raw}")
        recommended = rest
        restrictions.append((None, False, None, False))
    return MavenConstraint(raw, recommended, tuple(restrictions))


class MavenVersionScheme:
    def validate(self, raw: str) -> None:
        if not _valid(raw):
            raise InvalidVersion(f"{raw!r}: a Maven version is not empty and has no whitespace or control characters")

    def sort_key(self, raw: str) -> bytes:
        if not _valid(raw):
            return fallback_sort_key(raw)
        return _key_items(_parse(raw).items)

    def is_prerelease(self, raw: str) -> bool:
        return _valid(raw) and _prerelease(_parse(raw).items)

    def bump(self, previous: str | None, current: str) -> Bump | None:
        if previous is None:
            return None
        if not (_valid(previous) and _valid(current)):
            return "other"
        if self.is_prerelease(current):
            return "pre"
        if compare(current, previous) <= 0:
            return "other"
        prev, cur = _numbers(_parse(previous)), _numbers(_parse(current))
        if cur[0] != prev[0]:
            return "major"
        if cur[1] != prev[1]:
            return "minor"
        return "patch"                             # a change in the third number, a fourth one or a qualifier

    def constraint(self, raw: str) -> MavenConstraint:
        return parse_range(raw)
