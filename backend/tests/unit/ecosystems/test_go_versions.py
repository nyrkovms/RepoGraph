"""Go module versions against golang.org/x/mod itself (IMPL H1): tests/fixtures/go/semver/expected.json is written by
scripts/generate_go_semver_vectors.sh from `semver` and `module`, never by hand."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.go.versions import (
    GoVersionScheme,
    compare,
    is_incompatible,
    is_pseudo,
    major,
    parse,
    pseudo_revision,
    pseudo_time,
)

EXPECTED = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "go" / "semver" / "expected.json").read_text())
ENTRIES: dict[str, dict] = EXPECTED["versions"]
FULL = EXPECTED["compare_versions"]
SCHEME = GoVersionScheme()


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def test_the_vectors_are_big_enough() -> None:
    assert len(FULL) >= 50 and len(ENTRIES) >= 100
    assert any(e["IsPseudoVersion"] for e in ENTRIES.values())
    assert any(e["IsValid"] and not e["Full"] for e in ENTRIES.values())


@pytest.mark.parametrize(("version", "entry"), sorted(ENTRIES.items()))
def test_validity_is_go_validity_in_full_form(version: str, entry: dict) -> None:
    # Go's rule, as recorded: IsValid, and Canonical (which drops the build) plus the build is the input itself.
    # `v1` and `v1.2` are IsValid but Canonical rewrites them to `v1.0.0` and `v1.2.0`, so they are not module versions.
    assert entry["Full"] == (entry["IsValid"] and entry["Canonical"] + entry["Build"] == version)
    assert entry["CanonicalIsSelf"] <= entry["Full"]
    if entry["Full"]:
        SCHEME.validate(version)
    else:
        with pytest.raises(InvalidVersion):
            SCHEME.validate(version)


def test_precedence_and_sort_key_match_go_on_every_pair() -> None:
    mismatches = []
    for i, a in enumerate(FULL):
        for j, b in enumerate(FULL):
            expected = EXPECTED["compare"][i][j]
            by_key = _sign((SCHEME.sort_key(a) > SCHEME.sort_key(b)) - (SCHEME.sort_key(a) < SCHEME.sort_key(b)))
            if (compare(a, b), by_key) != (expected, expected):
                mismatches.append((a, b, expected, compare(a, b), by_key))
    assert mismatches == [], mismatches[:10]
    assert len(FULL) ** 2 >= 3000


def test_build_metadata_does_not_order_versions() -> None:
    assert SCHEME.sort_key("v2.0.0+incompatible") == SCHEME.sort_key("v2.0.0")
    assert SCHEME.sort_key("v1.0.0+build.5") == SCHEME.sort_key("v1.0.0+001")
    assert EXPECTED["compare"][FULL.index("v2.0.0")][FULL.index("v2.0.0+incompatible")] == 0


def test_pseudo_versions_sort_as_prereleases() -> None:
    older, newer = "v1.2.3-0.20191109021931-daa7c04131f5", "v1.2.3-0.20200101000000-abcdefabcdef"
    assert SCHEME.sort_key("v1.2.2") < SCHEME.sort_key(older) < SCHEME.sort_key(newer) < SCHEME.sort_key("v1.2.3")
    assert SCHEME.sort_key("v0.0.0-20191109021931-daa7c04131f5") < SCHEME.sort_key("v0.0.0-20200101000000-abcdefabcdef")


def test_fallback_key_sorts_below_valid_versions() -> None:
    for junk in ("latest", "v1", "v1.2", "1.2.3", ""):
        assert SCHEME.sort_key(junk).startswith(b"\x00")
        assert SCHEME.sort_key(junk) < SCHEME.sort_key("v0.0.0-0")


@pytest.mark.parametrize(("version", "entry"), sorted(ENTRIES.items()))
def test_is_prerelease_matches_go(version: str, entry: dict) -> None:
    expected = entry["Full"] and (entry["Prerelease"] != "" or entry["IsPseudoVersion"])
    assert SCHEME.is_prerelease(version) is expected


def test_bump() -> None:
    cases = [
        ("v1.2.3", "v2.0.0", "major"), ("v1.2.3", "v1.3.0", "minor"), ("v1.2.3", "v1.2.4", "patch"),
        ("v1.2.3", "v1.2.4-beta", "pre"), ("v1.2.3", "v1.2.3", "other"), ("v1.2.3", "v1.2.2", "other"),
        ("v1.2.3", "junk", "other"), ("junk", "v1.2.3", "other"), ("v1.2", "v1.3.0", "other"),
        ("v1.2.3", "v2.0.0+incompatible", "major"), ("v2.0.0", "v2.0.0+incompatible", "other"),
        ("v1.2.3", "v1.2.4-0.20191109021931-daa7c04131f5", "pre"),
        ("v1.2.3", "v0.0.0-20191109021931-daa7c04131f5", "pre"),
        ("v0.0.0-20191109021931-daa7c04131f5", "v1.0.0", "major"),
        ("v1.2.3-beta", "v1.2.3", "patch"), ("v1.0.0-rc.1", "v1.0.0", "patch"),
    ]
    assert [SCHEME.bump(p, c) for p, c, _ in cases] == [want for _, _, want in cases]
    assert SCHEME.bump(None, "v1.0.0") is None


def test_constraint_is_a_minimum_version_as_go_compares() -> None:
    mismatches = []
    for i, raw in enumerate(FULL):
        constraint = SCHEME.constraint(raw)
        assert constraint.raw() == raw
        for j, version in enumerate(FULL):
            # contains(v) is Compare(v, raw) >= 0, and compare[j][i] is Go's Compare(v, raw)
            if constraint.contains(version) != (EXPECTED["compare"][j][i] >= 0):
                mismatches.append((raw, version))
    assert mismatches == [], mismatches[:10]


def test_constraint_rejects_what_is_not_a_module_version() -> None:
    for raw in ("v1", "v1.2", "1.2.3", ">=v1.2.3", "latest", "v1.2.3-"):
        with pytest.raises(InvalidVersion):
            SCHEME.constraint(raw)
    # A root asked without a version (`go get path@latest`): any module version.
    for raw in ("", "*", " * "):
        anything = SCHEME.constraint(raw)
        assert anything.contains("v0.0.1") and anything.contains("v2.0.0+incompatible") and not anything.contains("x")
    constraint = SCHEME.constraint("v1.2.3")
    assert not constraint.contains("v1.2") and not constraint.contains("junk")
    assert constraint.contains("v1.2.3+incompatible") and constraint.contains("v2.0.0")


@pytest.mark.parametrize(("version", "entry"), sorted((k, e) for k, e in ENTRIES.items() if e["Full"]))
def test_pseudo_helpers_match_go(version: str, entry: dict) -> None:
    assert is_pseudo(version) is entry["IsPseudoVersion"]
    expected_time = entry["PseudoVersionTime"]
    assert pseudo_time(version) == (None if expected_time is None else datetime.fromisoformat(expected_time))
    assert pseudo_revision(version) == entry["PseudoVersionRev"]


def test_pseudo_helpers_on_versions_that_are_not_module_versions() -> None:
    for raw in ("v1", "v1.2", "junk", "", "v0.0.0-20191109021931-daa7c04131f5.1"):
        assert not is_pseudo(raw) and pseudo_time(raw) is None and pseudo_revision(raw) is None
    assert pseudo_time("v0.0.0-20191109021931-daa7c04131f5") == datetime(2019, 11, 9, 2, 19, 31, tzinfo=UTC)
    assert pseudo_time("v0.0.0-20190230000000-abcdefabcdef") is None                 # no 30 February
    assert pseudo_revision("v0.0.0-20190230000000-abcdefabcdef") == "abcdefabcdef"


@pytest.mark.parametrize(("version", "entry"), sorted((k, e) for k, e in ENTRIES.items() if e["Full"]))
def test_incompatible_and_major_match_go(version: str, entry: dict) -> None:
    assert is_incompatible(version) is entry["HasIncompatibleSuffix"]
    assert f"v{major(version)}" == entry["Major"]


def test_helpers_reject_or_ignore_invalid_input() -> None:
    assert not is_incompatible("v2+incompatible") and not is_incompatible("junk")
    with pytest.raises(InvalidVersion):
        major("v1")


def test_numbers_have_no_upper_limit_up_to_the_key_size() -> None:
    assert parse("v18446744073709551616.0.0").major == 2**64
    assert SCHEME.sort_key("v18446744073709551616.0.0") > SCHEME.sort_key("v18446744073709551615.0.0")
    with pytest.raises(InvalidVersion):
        parse(f"v{'9' * 256}.0.0")
    with pytest.raises(InvalidVersion):
        parse(f"v1.0.0-{'9' * 256}")
    parse(f"v{'9' * 255}.0.0")
