"""Helpers shared by the version schemes (SPEC-MULTI-ECOSYSTEM.md §5.5, IMPL T15)."""

from __future__ import annotations


def encode_number(digits: str) -> bytes:
    """Length-prefixed decimal: byte order of the result equals numeric order of arbitrarily large integers.

    The first byte is the digit count (>= 1), so a key built from numbers never starts with 0x00.
    """
    digits = digits.lstrip("0") or "0"
    if len(digits) > 255:
        raise ValueError(f"number has too many digits to encode: {len(digits)}")
    return bytes([len(digits)]) + digits.encode()


def fallback_sort_key(raw: str) -> bytes:
    """Key for a version the scheme rejects: below every valid key (which never starts with 0x00)."""
    return b"\x00" + raw.encode()


def semver_key(triple: tuple[int, int, int], prerelease: tuple[int | str, ...]) -> bytes:
    """SemVer 2.0 precedence as bytes (npm, crates): numeric prerelease identifiers by value, below alphanumeric ones.

    Ported from design/schema-check/sortkey_proto.py, checked against node-semver pairwise.
    """
    out = b"".join(encode_number(str(n)) for n in triple)
    if not prerelease:
        return out + b"\x02"                       # a release sorts after every prerelease of it
    out += b"\x01"
    for ident in prerelease:
        if isinstance(ident, int):
            out += b"\x01" + encode_number(str(ident))
        else:
            out += b"\x02" + ident.encode() + b"\x00"
    return out + b"\x00"                           # fewer identifiers sort first
