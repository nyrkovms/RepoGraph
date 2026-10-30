"""NuGet versions and ranges against NuGet.Versioning itself (IMPL I1): tests/fixtures/nuget/versioning/expected.json is
written by scripts/generate_nuget_version_vectors.sh from the library NuGet uses, never by hand."""

import json
from pathlib import Path

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.nuget.versions import NuGetVersionScheme, normalize, parse

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "nuget" / "versioning"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
INPUTS = json.loads((FIXTURES / "inputs.json").read_text())
VERSIONS: dict[str, dict] = EXPECTED["versions"]
RANGES: dict[str, dict | None] = EXPECTED["ranges"]
SCHEME = NuGetVersionScheme()


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def test_the_vectors_are_big_enough() -> None:
    assert sum(1 for v in VERSIONS.values() if v["valid"]) >= 50
    assert sum(1 for v in VERSIONS.values() if not v["valid"]) >= 20
    assert len(RANGES) >= 40 and sum(1 for r in RANGES.values() if r is None) >= 10
    assert any(v["valid"] and v["revision"] for v in VERSIONS.values())


@pytest.mark.parametrize("version", sorted(VERSIONS))
def test_validity_normalization_and_numbers_match_nuget(version: str) -> None:
    expected = VERSIONS[version]
    if not expected["valid"]:
        with pytest.raises(InvalidVersion):
            SCHEME.validate(version)
        return
    SCHEME.validate(version)
    assert normalize(version) == expected["normalized"]
    assert SCHEME.is_prerelease(version) is expected["is_prerelease"]
    parsed = parse(version)
    assert parsed.numbers == (expected["major"], expected["minor"], expected["patch"], expected["revision"])
    assert list(parsed.labels) == expected["release_labels"]


def test_order_and_sort_key_match_nuget_on_every_pair() -> None:
    versions = EXPECTED["compare_versions"]
    mismatches = []
    for i, a in enumerate(versions):
        for j, b in enumerate(versions):
            expected = EXPECTED["compare"][i][j]
            ours = parse(a).compare(parse(b))
            ka, kb = SCHEME.sort_key(a), SCHEME.sort_key(b)
            by_key = (ka > kb) - (ka < kb)
            if (ours, by_key) != (expected, expected):
                mismatches.append((a, b, expected, ours, by_key))
    assert mismatches == [], mismatches[:10]
    assert len(versions) ** 2 >= 10000


def test_equal_versions_have_equal_keys() -> None:
    keys = {SCHEME.sort_key(v) for v in ("1", "1.0", "1.0.0", "1.0.0.0", "01.00.0", "1.0.0+meta", " 1.0.0 ")}
    assert len(keys) == 1
    assert SCHEME.sort_key("1.0.0-Beta") == SCHEME.sort_key("1.0.0-beta") != SCHEME.sort_key("1.0.0-beta2")
    assert SCHEME.sort_key("1.0.0.1") > SCHEME.sort_key("1.0.0") and SCHEME.sort_key("1.0.0-rc") < SCHEME.sort_key("1.0.0")


@pytest.mark.parametrize("requirement", INPUTS["ranges"])
def test_range_matches_nuget(requirement: str) -> None:
    expected = RANGES[requirement]
    if expected is None:
        with pytest.raises(InvalidVersion):
            SCHEME.constraint(requirement)
        return
    constraint = SCHEME.constraint(requirement)
    assert constraint.raw() == requirement
    assert (constraint.min, constraint.min_inclusive, constraint.max, constraint.max_inclusive) == (
        expected["min"], expected["min_inclusive"], expected["max"], expected["max_inclusive"])
    assert constraint.floating == expected["floating"]
    wrong = {v: want for v, want in expected["satisfies"].items() if constraint.contains(v) != want}
    assert wrong == {}, f"{requirement!r}: NuGet says {wrong}"
    pools = EXPECTED["pools"]
    for pool, index in zip(pools, expected["best_match"], strict=True):
        best = constraint.best_match(pool)
        assert (None if best is None else pool.index(best)) == index, (requirement, pool)


def test_floating_ranges_pick_the_highest_and_fixed_ones_the_lowest() -> None:
    pool = ["0.9.0", "1.0.0", "1.5.0", "1.6.0-beta", "2.0.0"]
    assert SCHEME.constraint("1.*").best_match(pool) == "1.5.0"
    assert SCHEME.constraint("1.*-*").best_match(pool) == "1.6.0-beta"
    assert SCHEME.constraint("1.0").best_match(pool) == "1.0.0"
    assert SCHEME.constraint("[1.0,2.0)").best_match(pool) == "1.0.0"
    assert SCHEME.constraint("(1.0,)").best_match(pool) == "1.5.0"
    assert SCHEME.constraint("[3.0,)").best_match(pool) is None
    assert SCHEME.constraint("1.0").best_match(["junk", "1.2"]) == "1.2"
    assert SCHEME.constraint("1.0").contains("junk") is False


def test_prerelease_is_not_excluded_by_a_range() -> None:
    assert SCHEME.constraint("[1.0,2.0)").contains("1.5.0-beta")
    assert not SCHEME.constraint("1.0").contains("1.0.0-beta")
    assert not SCHEME.is_prerelease("junk") and not SCHEME.is_prerelease("1.0.0+meta")


def test_bump_and_fallback_key() -> None:
    assert [SCHEME.bump("1.2.3", v) for v in ("2.0.0", "1.3.0", "1.2.4", "1.2.3.1", "1.2.4-beta", "1.2.3", "1.2.2", "junk")] == [
        "major", "minor", "patch", "patch", "pre", "other", "other", "other"]
    assert SCHEME.bump("1.2.3.4", "1.2.3.5") == "patch"
    assert SCHEME.bump(None, "1.0.0") is None
    assert SCHEME.bump("junk", "1.0.0") == "other"
    assert SCHEME.sort_key("not-a-version").startswith(b"\x00")
    assert SCHEME.sort_key("not-a-version") < SCHEME.sort_key("0.0.0")
