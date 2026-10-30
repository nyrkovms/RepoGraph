"""Cargo versions and requirements against the `semver` crate itself (IMPL G1): tests/fixtures/crates/semver/
expected.json is written by scripts/generate_cargo_semver_vectors.sh from the crate Cargo uses, never by hand."""

import json
from pathlib import Path

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.crates.versions import CargoConstraint, CratesVersionScheme, compatibility, parse

EXPECTED = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "crates" / "semver" / "expected.json").read_text())
SCHEME = CratesVersionScheme()


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


@pytest.mark.parametrize(("version", "valid"), sorted(EXPECTED["valid"].items()))
def test_validity_matches_the_semver_crate(version: str, valid: bool) -> None:
    if valid:
        SCHEME.validate(version)
    else:
        with pytest.raises(InvalidVersion):
            SCHEME.validate(version)


def test_precedence_and_sort_key_match_the_semver_crate_on_every_pair() -> None:
    versions = EXPECTED["compare_versions"]
    mismatches = []
    for i, a in enumerate(versions):
        for j, b in enumerate(versions):
            expected = EXPECTED["compare"][i][j]
            ours = parse(a).compare(parse(b))
            by_key = _sign((SCHEME.sort_key(a) > SCHEME.sort_key(b)) - (SCHEME.sort_key(a) < SCHEME.sort_key(b)))
            if (ours, by_key) != (expected, expected):
                mismatches.append((a, b, expected, ours, by_key))
    assert mismatches == [], mismatches[:10]
    assert len(versions) ** 2 >= 3000


@pytest.mark.parametrize("requirement", sorted(EXPECTED["ranges"]))
def test_requirement_matches_the_semver_crate(requirement: str) -> None:
    expected = EXPECTED["ranges"][requirement]
    if expected is None:
        with pytest.raises(InvalidVersion):
            SCHEME.constraint(requirement)
        return
    constraint = SCHEME.constraint(requirement)
    wrong = {v: want for v, want in expected.items() if constraint.contains(v) != want}
    assert wrong == {}, f"{requirement!r}: the semver crate says {wrong}"


def test_prerelease_bump_and_fallback_key() -> None:
    assert SCHEME.is_prerelease("1.0.0-rc.1") and not SCHEME.is_prerelease("1.0.0+build")
    assert not SCHEME.is_prerelease("v1.0.0")
    assert [SCHEME.bump("1.2.3", v) for v in ("2.0.0", "1.3.0", "1.2.4", "1.2.4-beta", "1.2.3", "junk")] == [
        "major", "minor", "patch", "pre", "other", "other"]
    assert SCHEME.bump(None, "1.0.0") is None
    assert SCHEME.sort_key("not-a-version").startswith(b"\x00")
    assert SCHEME.sort_key("1.0.0+a") == SCHEME.sort_key("1.0.0")


@pytest.mark.parametrize(("version", "group"), [
    ("1.2.3", "1"), ("1.0.0-alpha", "1"), ("0.3.9", "0.3"), ("0.0.3", "0.0.3"), ("0.0.0", "0.0.0"), ("10.0.0", "10")])
def test_compatibility_classes(version: str, group: str) -> None:
    assert compatibility(version) == group


@pytest.mark.parametrize(("requirement", "pinned"), [
    ("=1.2.3", "1.2.3"), ("= 1.2.3-beta.2", "1.2.3-beta.2"), ("=1.2", None), ("1.2.3", None), ("=1.2.3, <2", None),
    ("*", None)])
def test_pinned(requirement: str, pinned: str | None) -> None:
    constraint = SCHEME.constraint(requirement)
    assert isinstance(constraint, CargoConstraint) and constraint.pinned() == pinned
    assert constraint.raw() == requirement and not constraint.contains("junk")
