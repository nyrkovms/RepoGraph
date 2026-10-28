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
