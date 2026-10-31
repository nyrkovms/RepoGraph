"""RPM versions and capabilities against rpm itself (IMPL K1): tests/fixtures/rpm/versions/expected.json is written by
scripts/generate_rpm_version_vectors.sh from rpm.labelCompare and rpmdsCompare (rpm 4.16, rockylinux:9), never by hand."""

import itertools
import json
from pathlib import Path

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.rpm.versions import RpmVersionScheme, compare, parse, parse_constraint, vercmp

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "rpm" / "versions"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
INPUTS = json.loads((FIXTURES / "inputs.json").read_text())
CONSTRAINTS: dict[str, dict] = EXPECTED["constraints"]
SCHEME = RpmVersionScheme()
SIGN = {"<": -1, "=": 0, ">": 1}


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def test_the_vectors_are_big_enough() -> None:
    assert len(INPUTS["versions"]) >= 88 and len(INPUTS["versions"]) ** 2 >= 7000
    assert len(CONSTRAINTS) >= 30 and len(INPUTS["candidates"]) >= 40
    assert EXPECTED["rpm_version"].startswith("4.16")
    assert EXPECTED["compare_versions"] == INPUTS["versions"]
    assert all(len(row) == len(INPUTS["versions"]) for row in EXPECTED["compare"])


def test_order_matches_rpm_on_every_pair() -> None:
    versions = EXPECTED["compare_versions"]
    mismatches = [
        (a, b, EXPECTED["compare"][i][j], _sign(compare(a, b)))
        for i, a in enumerate(versions) for j, b in enumerate(versions)
        if SIGN[EXPECTED["compare"][i][j]] != compare(a, b)]
    assert mismatches == [], mismatches[:10]


def test_sort_key_matches_rpm_on_every_pair() -> None:
    versions = EXPECTED["compare_versions"]
    keys = [SCHEME.sort_key(v) for v in versions]
    mismatches = [
        (versions[i], versions[j])
        for i, j in itertools.product(range(len(versions)), repeat=2)
        if _sign((keys[i] > keys[j]) - (keys[i] < keys[j])) != SIGN[EXPECTED["compare"][i][j]]]
    assert mismatches == [], mismatches[:10]
    assert all(not key.startswith(b"\x00") for key in keys)


def test_equal_versions_have_equal_keys() -> None:
    same = [("1.0-1", "0:1.0-1", "00:1.0-1", "1.0-1.", "1_0-1", "1.00-1", "01.0-001", "1..0-1", "1.0-1\u00e9"),
            ("1.0~rc1-1", "1.0~rc01-1", "1.0~rc1.-1")]
    for group in same:
        assert len({SCHEME.sort_key(v) for v in group}) == 1, group
        assert {compare(group[0], v) for v in group} == {0}
    assert SCHEME.sort_key("1.0^git1-1") != SCHEME.sort_key("1.0^git1^-1")


def test_the_release_is_a_part_of_the_order_and_a_missing_one_is_the_lowest() -> None:
    assert compare("1.0", "1.0-1") < 0 < compare("1.0-1", "1.0")
    assert compare("1.0-", "1.0") > 0 and compare("1.0-1", "1.0-") > 0
    assert SCHEME.sort_key("1.0") < SCHEME.sort_key("1.0-~") < SCHEME.sort_key("1.0-") < SCHEME.sort_key("1.0-1")
    assert compare("1:1.0-1", "2.0-1") > 0 and compare("0:2", "2") == 0


def test_the_long_numbers_compare_as_numbers() -> None:
    big, bigger = "1." + "9" * 300 + "-1", "1.1" + "0" * 300 + "-1"
    assert compare(big, bigger) < 0 and SCHEME.sort_key(big) < SCHEME.sort_key(bigger)
    assert SCHEME.sort_key("1." + "0" * 300 + "5-1") == SCHEME.sort_key("1.5-1")
    assert SCHEME.sort_key("1." + "9" * 254 + "-1") < SCHEME.sort_key("1.1" + "0" * 254 + "-1") < SCHEME.sort_key(big)
    assert SCHEME.sort_key("1:1.0") > SCHEME.sort_key("0:" + "9" * 40 + "-1")
    assert SCHEME.sort_key("9" * 300 + ":1") > SCHEME.sort_key("9" * 299 + ":1")


def test_vercmp_follows_the_rpm_rules() -> None:
    assert vercmp("1.0~rc1", "1.0") < 0 < vercmp("1.0", "1.0~rc1")
    assert vercmp("1.0~", "1.0~~") > 0 and vercmp("~", "") < 0
    assert vercmp("1.0", "1.0^") < 0 and vercmp("1.0^", "1.0.1") < 0                # 1.0 < 1.0^ < 1.0.1
    assert vercmp("1.0^1", "1.0^a") > 0 and vercmp("1.0^", "1.0~") > 0
    assert vercmp("1.0a", "1.0.1") < 0 and vercmp("1.0a", "1.0ab") < 0 and vercmp("B", "a") < 0
    assert vercmp("1.01", "1.1") == 0 and vercmp("1.0", "1.0.") == 0 and vercmp("1", "1.0") < 0


@pytest.mark.parametrize("raw", ["", " ", "1.0 ", " 1.0", "1.0-1 2", "1.0\t", "1.0\n-1", "1.0\x00", "1.0\x7f", "-1", "1:", "1:-1", ":1.0",
                                 "a:1.0", "1a:1.0-1", "1.5:1.0", "-", "1.0 -1", " 1.0"])
def test_validate_rejects(raw: str) -> None:
    with pytest.raises(InvalidVersion):
        SCHEME.validate(raw)
    assert SCHEME.sort_key(raw) == b"\x00" + raw.encode()
    assert not SCHEME.is_prerelease(raw) and not parse_constraint("").contains(raw)


@pytest.mark.parametrize("raw", ["1", "1.0", "1.0-1", "0:1.0-1", "1:1.0", "1.0-", "1:2:3-1", "1.0-1-2", "~1", "^", "a", "1.0-1:2", "1:1.0-1.el9_4",
                                 "5.15.0-200.131.27.el9uek", "1.0é-1", "99999999999999999999:1"])
def test_validate_accepts(raw: str) -> None:
    SCHEME.validate(raw)


def test_parse_splits_like_rpm() -> None:
    assert (parse("1.2.3-4").epoch, parse("1.2.3-4").version, parse("1.2.3-4").release) == ("0", "1.2.3", "4")
    assert (parse("007:1.2").epoch, parse("007:1.2").version, parse("007:1.2").release) == ("7", "1.2", None)
    assert (parse("1:2:3-1").epoch, parse("1:2:3-1").version) == ("1", "2:3")
    assert (parse("1.0-1-2").version, parse("1.0-1-2").release) == ("1.0-1", "2")
    assert (parse("1.0-1:2").epoch, parse("1.0-1:2").version, parse("1.0-1:2").release) == ("0", "1.0", "1:2")
    assert parse("1.0-").release == "" and parse("1.0").release is None


def test_prerelease_is_a_tilde_in_the_version() -> None:
    assert SCHEME.is_prerelease("1.0~rc1-1") and SCHEME.is_prerelease("1:1.0~beta") and SCHEME.is_prerelease("1.0~rc1^git1-1")
    assert not SCHEME.is_prerelease("1.0-1") and not SCHEME.is_prerelease("1.0-0.1~rc1") and not SCHEME.is_prerelease("1.0^git1-1")
    assert not SCHEME.is_prerelease("1.0-1.module+el8.9.0+19500+aaaaaaaa") and not SCHEME.is_prerelease("junk x")


def test_bump() -> None:
    # A new release of the same version is a patch (SPEC §5.5): a rebuild or a backported fix.
    cases = {
        "3.0.7-27.el9": [("4.0.0-1.el9", "major"), ("3.1.0-1.el9", "minor"), ("3.0.8-1.el9", "patch"), ("3.0.7.1-1", "patch"),
                         ("3.0.7-28.el9", "patch"), ("3.0.7-27.el9_4", "patch"), ("3.0.7-27.el9", "other"), ("3.0.6-1", "other"),
                         ("1:3.0.7-27.el9", "other"), ("1:4.0.0-1", "other"), ("3.0.8~rc1-1", "pre"), ("4.0~beta-1", "pre"),
                         ("3.0.7a-1", "patch"), ("3.0.07-28.el9", "patch"), ("2.9.9-1", "other")],
        "1.1.1k-9.el9": [("1.1.1l-1.el9", "patch"), ("1.1.2-1", "patch"), ("1.2-1", "minor"), ("1.1.1k-12.el9_4", "patch")],
        "1.0~rc1-1": [("1.0-1", "patch"), ("1.0~rc2-1", "pre"), ("1.1-1", "minor"), ("2.0-1", "major")],
        "5.14.0-427.13.1.el9_4": [("5.14.0-503.14.1.el9_5", "patch"), ("5.15.0-1.el9", "minor")],
    }
    for previous, changes in cases.items():
        assert [(c, SCHEME.bump(previous, c)) for c, _ in changes] == changes, previous
    assert SCHEME.bump(None, "1.0-1") is None
    assert SCHEME.bump("junk x", "1.0-1") == "other" and SCHEME.bump("1.0-1", "junk x") == "other"
    assert SCHEME.bump("1.0", "2.0") == "major" and SCHEME.bump("a-1", "b-1") == "patch" and SCHEME.bump("1", "10") == "major"


@pytest.mark.parametrize("raw", list(CONSTRAINTS))
def test_constraint_matches_rpm(raw: str) -> None:
    expected = CONSTRAINTS[raw]
    constraint = SCHEME.constraint(raw)
    assert constraint.raw() == raw
    assert constraint.flag == expected["flag"]
    assert constraint.evr == (None if expected["evr"] is None else parse(expected["evr"]))
    wrong = {v: want for v, want in expected["contains"].items() if constraint.contains(v) != want}
    assert wrong == {}, f"{raw!r}: rpm says {wrong}"
    for pool, index in zip(INPUTS["pools"], expected["best_match"], strict=True):
        best = constraint.best_match(pool)
        assert (None if best is None else pool.index(best)) == index, (raw, pool)


def test_the_constraint_forms_mean_the_same_as_in_rpm() -> None:
    same = [(">= 1.2-3", "GE 1.2-3", ">=1.2-3", "=> 1.2-3", ">=  1.2-3 "), ("= 1.0", "EQ 1.0", "== 1.0", "=1.0"),
            ("<= 1:1.0", "LE 1:1.0", "=< 1:1.0", "<=1:1.0"), ("< 2", "LT 2", "<2"), ("> 1.0", "GT 1.0", ">1.0"), ("", "*", " * ", "  ")]
    for group in same:
        constraints = [parse_constraint(raw) for raw in group]
        assert len({(c.flag, c.evr) for c in constraints}) == 1, group
        answers = {tuple(c.contains(v) for v in INPUTS["candidates"]) for c in constraints}
        assert len(answers) == 1, group


@pytest.mark.parametrize("raw", ["1.2", "foo", ">=", "GE", "GE1.2", "ge 1.2", ">= 1.2 1.3", ">> 1.2", "!= 1.2", ">= a:1.0", ">= -1", ">= 1:",
                                 "= 1.0 -1", "(foo >= 1)", "~> 1.0", ">= \t", "EQ  "])
def test_constraint_rejects(raw: str) -> None:
    with pytest.raises(InvalidVersion):
        SCHEME.constraint(raw)


def test_constraints_follow_rpmdscompare_on_epoch_and_release() -> None:
    assert parse_constraint(">= 1.2").contains("1.2-0.1") and parse_constraint("= 1.2").contains("1.2-5")
    assert not parse_constraint(">= 1.2-3").contains("1.2-2") and parse_constraint(">= 1.2-3").contains("1.2")
    assert parse_constraint("> 1.0-1").contains("1.0") and parse_constraint("< 1.0-1").contains("1.0")   # a provide without a release
    assert not parse_constraint("> 1.0-1").contains("1.0-1") and not parse_constraint("> 1.0").contains("1.0-5")
    assert parse_constraint("= 1.0").contains("0:1.0-1") and not parse_constraint("= 1.0").contains("1:1.0")
    assert parse_constraint(">= 1.0").contains("1:0.1") and parse_constraint("<= 1:1.0").contains("9.9")
    assert parse_constraint(">= 1.0~rc1").contains("1.0") and not parse_constraint("> 1.0").contains("1.0~rc1")
    assert not parse_constraint("= 1.0").contains("1.0-5-5")                    # the version is `1.0-5`
    assert parse_constraint("").contains("1:9") and not parse_constraint("").contains("junk x")


def test_best_match_is_the_highest_contained_and_skips_junk() -> None:
    pool = ["0.9-1", "1.0-1", "junk x", "1.2-3", "1.2-4", "1:0.5-1", "1.2-04", "2.0-1"]
    assert parse_constraint(">= 1.0").best_match(pool) == "1:0.5-1"
    assert parse_constraint("< 2").best_match(pool) == "1.2-4"           # 1:0.5 has the higher epoch
    assert parse_constraint("< 1:0").best_match(pool) == "2.0-1"
    assert parse_constraint("= 1.2").best_match(pool) == "1.2-4"
    assert parse_constraint("= 1.2-3").best_match(["1.2-03", "1.2-3"]) == "1.2-03"           # the first of equal versions
    assert parse_constraint("> 5").best_match(pool) == "1:0.5-1"
    assert parse_constraint("> 1:5").best_match(pool) is None and parse_constraint("").best_match([]) is None
    assert parse_constraint("").best_match(["junk x", "1.0", "1.0~rc1"]) == "1.0"


def test_fallback_key_sorts_below_every_version() -> None:
    assert SCHEME.sort_key("not a version") < SCHEME.sort_key("~") < SCHEME.sort_key("0-0")
