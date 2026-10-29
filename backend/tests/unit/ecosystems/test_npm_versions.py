"""npm versions and ranges against node-semver itself (IMPL F1): tests/fixtures/npm/semver/expected.json is written by
scripts/generate_semver_vectors.sh from node-semver (the version npm 10 bundles), never by hand."""

import json
from pathlib import Path

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.npm.versions import NpmVersionScheme, parse

EXPECTED = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "npm" / "semver" / "expected.json").read_text())
SCHEME = NpmVersionScheme()


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


@pytest.mark.parametrize(("version", "valid"), sorted(EXPECTED["valid"].items()))
def test_validity_matches_node_semver(version: str, valid: bool) -> None:
    if valid:
        SCHEME.validate(version)
    else:
        with pytest.raises(InvalidVersion):
            SCHEME.validate(version)


def test_comparison_and_sort_key_match_node_semver_on_every_pair() -> None:
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


@pytest.mark.parametrize("range_", sorted(EXPECTED["ranges"]))
def test_range_matches_node_semver(range_: str) -> None:
    expected = EXPECTED["ranges"][range_]
    if expected is None:
        with pytest.raises(InvalidVersion):
            SCHEME.constraint(range_)
        return
    constraint = SCHEME.constraint(range_)
    wrong = {v: want for v, want in expected.items() if constraint.contains(v) != want}
    assert wrong == {}, f"{range_!r}: node-semver says {wrong}"


def test_prerelease_and_bump() -> None:
    assert SCHEME.is_prerelease("1.0.0-rc.1") and not SCHEME.is_prerelease("1.0.0+build")
    assert [SCHEME.bump("1.2.3", v) for v in ("2.0.0", "1.3.0", "1.2.4", "1.2.4-beta", "1.2.3")] == [
        "major", "minor", "patch", "pre", "other"]
    assert SCHEME.bump(None, "1.0.0") is None
    assert SCHEME.sort_key("not-a-version").startswith(b"\x00")
