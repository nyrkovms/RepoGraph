"""Maven versions and ranges against maven-artifact itself (IMPL J1): tests/fixtures/maven/versions/expected.json is
written by scripts/generate_maven_version_vectors.sh from ComparableVersion and VersionRange, never by hand."""

import itertools
import json
from pathlib import Path

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.maven.versions import MavenVersionScheme, canonical, compare, parse_range

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "maven" / "versions"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
INPUTS = json.loads((FIXTURES / "inputs.json").read_text())
RANGES: dict[str, dict] = EXPECTED["ranges"]
SCHEME = MavenVersionScheme()
SIGN = {"<": -1, "=": 0, ">": 1}


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _valid(version: str) -> bool:
    try:
        SCHEME.validate(version)
    except InvalidVersion:
        return False
    return True


def test_the_vectors_are_big_enough() -> None:
    assert len(INPUTS["versions"]) >= 80 and len(INPUTS["versions"]) ** 2 >= 10000
    assert len(RANGES) >= 40 and sum(1 for r in RANGES.values() if not r["valid"]) >= 10
    assert EXPECTED["tool"] == "maven-artifact 3.9.9"
    assert len(EXPECTED["compare"]) == len(EXPECTED["compare_versions"]) == len(INPUTS["versions"]) + len(INPUTS["edge_versions"])


@pytest.mark.parametrize("version", sorted(EXPECTED["canonical"]))
def test_canonical_matches_maven(version: str) -> None:
    assert canonical(version) == EXPECTED["canonical"][version]


def test_order_matches_maven_on_every_pair() -> None:
    versions = EXPECTED["compare_versions"]
    mismatches = [
        (a, b, EXPECTED["compare"][i][j], _sign(compare(a, b)))
        for i, a in enumerate(versions) for j, b in enumerate(versions)
        if SIGN[EXPECTED["compare"][i][j]] != compare(a, b)]
    assert mismatches == [], mismatches[:10]


def test_sort_key_matches_maven_on_every_pair() -> None:
    versions = [v for v in EXPECTED["compare_versions"] if _valid(v)]
    index = {v: i for i, v in enumerate(EXPECTED["compare_versions"])}
    keys = {v: SCHEME.sort_key(v) for v in versions}
    mismatches = []
    for a, b in itertools.product(versions, repeat=2):
        by_key = _sign((keys[a] > keys[b]) - (keys[a] < keys[b]))
        if by_key != SIGN[EXPECTED["compare"][index[a]][index[b]]]:
            mismatches.append((a, b))
    assert mismatches == [], mismatches[:10]
    assert len(versions) ** 2 >= 10000


def test_equal_versions_have_equal_keys() -> None:
    same = [("1", "1.0", "1.0.0", "01", "1.0.0.0", "1-0", "1.ga", "1.RELEASE", "1-final", "1.0-GA"),
            ("1.0-alpha1", "1.0-alpha-1", "1-a1", "1.0-ALPHA-1"), ("1.0-rc1", "1.0-cr1", "1-RC-1"), ("1.0-sp1", "1-SP-1")]
    for group in same:
        assert len({SCHEME.sort_key(v) for v in group}) == 1, group
    assert SCHEME.sort_key("1.0-SNAPSHOT") != SCHEME.sort_key("1.0")


def test_maven_order_is_not_transitive_and_compare_follows_it_exactly() -> None:
    """`1-alpha < 1 < 1.sp.5 < 1-alpha`: no byte key can follow a cycle, so these versions are kept out of the key test."""
    versions = EXPECTED["cycle_versions"]
    rows = EXPECTED["cycle_compare"]
    index = {v: i for i, v in enumerate(versions)}
    for a, b in itertools.product(versions, repeat=2):
        assert SIGN[rows[index[a]][index[b]]] == compare(a, b), (a, b)
    assert compare("1-alpha", "1") < 0 < compare("1.sp.5", "1") and compare("1.sp.5", "1-alpha") < 0
    cycles = [(a, b, c) for a, b, c in itertools.permutations(versions, 3)
              if SIGN[rows[index[a]][index[b]]] < 0 and SIGN[rows[index[b]][index[c]]] < 0 and SIGN[rows[index[c]][index[a]]] < 0]
    assert ("1-alpha", "1", "1.sp.5") in cycles


def test_the_order_of_everyday_versions() -> None:
    ordered = ["1.0-alpha", "1.0-alpha-2", "1.0-beta", "1.0-milestone", "1.0-rc", "1.0-SNAPSHOT", "1.0", "1.0-sp", "1.0-xyz", "1.0-1",
               "1.0.1", "1.1", "1.10", "2", "2147483648.0", "99999999999999999999.0"]
    keys = [SCHEME.sort_key(v) for v in ordered]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert all(compare(a, b) < 0 for a, b in itertools.pairwise(ordered))


def test_numbers_of_any_size_and_unknown_qualifiers() -> None:
    huge = "9" * 400
    assert SCHEME.sort_key(f"1.{huge}") > SCHEME.sort_key("1." + "9" * 300) > SCHEME.sort_key("1.2147483648")
    assert compare(f"1.{huge}", "1." + "9" * 300) > 0 and canonical(f"1.{huge}") == f"1.{huge}"
    assert SCHEME.sort_key("1.0-Foo") == SCHEME.sort_key("1.0-foo")
    assert SCHEME.sort_key("1.0-foo") < SCHEME.sort_key("1.0-foo1") < SCHEME.sort_key("1.0-fooo") < SCHEME.sort_key("1.0-fop")
    assert SCHEME.sort_key("1.0-sp") < SCHEME.sort_key("1.0-aaa") < SCHEME.sort_key("1.0-b-b")
    # UTF-16 code units, not code points: U+10000 (a surrogate pair) is below U+FFFF
    assert SCHEME.sort_key("1.0-\U00010000") < SCHEME.sort_key("1.0-￿")


@pytest.mark.parametrize("version", ["", " 1.0", "1.0 ", "1. 0", "1.0\t", "1.0\n", "1.0\x00", "1.0 ", " 1"])
def test_validate_rejects_the_empty_string_whitespace_and_control_characters(version: str) -> None:
    with pytest.raises(InvalidVersion):
        SCHEME.validate(version)
    assert SCHEME.sort_key(version).startswith(b"\x00")
    assert not SCHEME.is_prerelease(version)


def test_every_other_string_is_valid() -> None:
    for version in INPUTS["versions"]:
        SCHEME.validate(version)
        assert not SCHEME.sort_key(version).startswith(b"\x00")
    assert SCHEME.sort_key("not-a-version") > SCHEME.sort_key("") and SCHEME.sort_key("1.0")[:1] != b"\x00"


@pytest.mark.parametrize("version", ["1.0-alpha", "1.0-alpha1", "1.0-a1", "1.0-beta", "1.0-b2", "1.0-milestone", "1.0-m1", "1.0-rc", "1.0-RC1",
                                     "1.0-cr1", "1.0-SNAPSHOT", "1-snapshot", "1.0.0.M2", "2.0.0.CR1", "1.0.0.BUILD-SNAPSHOT",
                                     "3.0.0-alpha.1", "1.0-RC1-SNAPSHOT", "1.0-alpha-SNAPSHOT", "1.0-SNAPSHOT-alpha", "5.10.0-M1"])
def test_prerelease_qualifiers(version: str) -> None:
    assert SCHEME.is_prerelease(version)


@pytest.mark.parametrize("version", ["1.0", "1", "1.0.RELEASE", "1.0-ga", "1.0-final", "1.0-sp1", "1.0-foo", "1.0-1", "1.0-a", "1.0-m",
                                     "1.0.1", "5.2.1.RELEASE", "32.1.2-jre", "1.0-20230101.123456-1", "v1.0", "1.0+build5"])
def test_release_versions_are_not_prereleases(version: str) -> None:
    assert not SCHEME.is_prerelease(version)


def test_bump() -> None:
    cases = [("1.2.3", "2.0.0", "major"), ("1.2.3", "1.3.0", "minor"), ("1.2.3", "1.2.4", "patch"), ("1.2.3", "1.2.3.1", "patch"),
             ("1.2.3", "1.2.3-sp1", "patch"), ("1.2.3", "1.2.4-SNAPSHOT", "pre"), ("1.2.3", "1.2.4-rc1", "pre"), ("1.2.3", "1.2.3", "other"),
             ("1.2.3", "1.2.2", "other"), ("1.2.3", "1.2.3.RELEASE", "other"), ("1.2", "1.2.0.1", "patch"), ("1", "1.1", "minor"),
             ("1.0-rc1", "1.0", "patch"), ("1.0-rc1", "1.0.1", "patch"), ("1.0-1", "2.0", "major"), ("1.2.3", "2", "major"),
             ("1.2.3", "bogus", "other"), ("1.2.3", " 1.2.4", "other"), ("2147483648.0", "2147483649.0", "major")]
    assert [SCHEME.bump(previous, current) for previous, current, _ in cases] == [want for *_, want in cases]
    assert SCHEME.bump(None, "1.0") is None
    assert SCHEME.bump("", "1.0") == "other"
    assert SCHEME.bump("bogus", "1.0") == "major"       # a word is a valid version, a qualifier below 0.0.0


@pytest.mark.parametrize("spec", INPUTS["ranges"])
def test_range_matches_maven(spec: str) -> None:
    expected = RANGES[spec]
    if not expected["valid"]:
        with pytest.raises(InvalidVersion):
            SCHEME.constraint(spec)
        return
    constraint = SCHEME.constraint(spec)
    assert constraint.raw() == spec
    assert constraint.recommended == expected["recommended"]
    assert [list(r) for r in constraint.restrictions] == expected["restrictions"]
    wrong = {v: want for v, want in expected["contains"].items() if constraint.contains(v) != want}
    assert wrong == {}, f"{spec!r}: Maven says {wrong}"
    for pool, index in zip(EXPECTED["pools"], expected["best_match"], strict=True):
        best = constraint.best_match(pool)
        assert (None if best is None else pool.index(best)) == index, (spec, pool)


def test_ranges_in_use() -> None:
    soft = SCHEME.constraint("1.0")
    assert soft.recommended == "1.0" and soft.contains("0.1") and soft.contains("9.9") and soft.best_match(["1.0", "3.0", "2.0"]) == "3.0"
    exact = SCHEME.constraint("[1.0]")
    assert exact.recommended is None and exact.contains("1.0.0") and not exact.contains("1.0.1")
    assert exact.restrictions == (("1.0", True, "1.0", True),)
    pool = ["0.9", "1.0", "1.5", "2.0", "2.5-SNAPSHOT", "3.0"]
    assert SCHEME.constraint("[1.0,2.0)").best_match(pool) == "1.5"
    assert SCHEME.constraint("(,1.0]").best_match(pool) == "1.0"
    assert SCHEME.constraint("[1.0,)").best_match(pool) == "3.0"
    assert SCHEME.constraint("[1,2),[3,4)").best_match(pool) == "3.0"
    assert SCHEME.constraint("[4,)").best_match(pool) is None
    assert SCHEME.constraint("[1.0,2.0)").restrictions == (("1.0", True, "2.0", False),)
    assert SCHEME.constraint("(,1.0]").restrictions == ((None, False, "1.0", True),)
    assert SCHEME.constraint("[1.0,)").contains("1.0-SNAPSHOT") is False
    assert SCHEME.constraint("[1.0,)").contains("1.0")
    assert parse_range("[1.0,2.0)") is parse_range("[1.0,2.0)")


def test_best_match_skips_invalid_strings_and_keeps_the_first_of_equals() -> None:
    assert SCHEME.constraint("[1.0,)").best_match(["1.5", "", "1.0 ", "2 .0"]) == "1.5"
    assert SCHEME.constraint("[1.0,)").best_match(["1.0.0", "1", "1.0"]) == "1.0.0"
    assert SCHEME.constraint("[1.0,)").best_match([]) is None
