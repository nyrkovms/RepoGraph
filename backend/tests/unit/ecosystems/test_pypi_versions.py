import itertools

import pytest
from packaging.version import Version

from app.ecosystems.base import InvalidVersion, VersionScheme
from app.ecosystems.pypi.versions import PyPIVersionScheme
from app.ecosystems.versions_common import encode_number, fallback_sort_key

SCHEME = PyPIVersionScheme()

# 38 versions as in the design check (SPEC §5.5): dev/pre/post, local labels, epochs, zero padding.
SAMPLES = [
    "0.1",
    "0.9",
    "1.0.dev1",
    "1.0.dev2",
    "1.0a1",
    "1.0a2.dev1",
    "1.0a2",
    "1.0b1",
    "1.0b1.post1",
    "1.0rc1",
    "1.0rc1.dev3",
    "1.0",
    "1.0.0",
    "1.0.post1.dev1",
    "1.0.post1",
    "1.0.post2",
    "1.0+local",
    "1.0+cpu",
    "1.0+cu121",
    "1.0.post1+cu121",
    "1.0.1",
    "1.1",
    "1.10",
    "1.2",
    "2.0.0",
    "2.8.0",
    "2.8.0+cpu",
    "2.8.0+cu121",
    "2.8.0+cu121.1",
    "2.8.0+cu121.a",
    "2.8.0+1",
    "2.8.0+10",
    "10.0",
    "100.0.1",
    "1!0.5",
    "1!1.0",
    "1!1.0rc1",
    "2!0.1",
]


def test_scheme_implements_protocol() -> None:
    scheme: VersionScheme = PyPIVersionScheme()
    assert scheme.sort_key("1.0") > b""


def test_sample_list_size() -> None:
    assert len(SAMPLES) == 38


def test_sort_key_order_matches_packaging() -> None:
    compared = 0
    for left, right in itertools.product(SAMPLES, repeat=2):
        expected = (Version(left) > Version(right)) - (Version(left) < Version(right))
        key_left, key_right = SCHEME.sort_key(left), SCHEME.sort_key(right)
        actual = (key_left > key_right) - (key_left < key_right)
        assert actual == expected, (left, right)
        compared += 1
    assert compared == 38 * 38 == 1444


def test_sort_key_is_stable_for_equal_versions() -> None:
    assert SCHEME.sort_key("1.0") == SCHEME.sort_key("1.0.0")
    assert SCHEME.sort_key("1.0") == SCHEME.sort_key("1.0.0.0")
    assert SCHEME.sort_key("1.0RC1") == SCHEME.sort_key("1.0rc1")


def test_sort_key_handles_big_numbers() -> None:
    assert SCHEME.sort_key("9.0") < SCHEME.sort_key("10.0") < SCHEME.sort_key("1" + "0" * 40 + ".0")


@pytest.mark.parametrize("raw", ["", "not-a-version", "1..0", "v", "1.0-dev-x", "abc1.0", " "])
def test_invalid_versions_sort_below_every_valid_one(raw: str) -> None:
    key = SCHEME.sort_key(raw)

    assert key == fallback_sort_key(raw)
    assert all(key < SCHEME.sort_key(valid) for valid in SAMPLES)


def test_invalid_versions_order_among_themselves_by_raw_text() -> None:
    assert SCHEME.sort_key("aaa") < SCHEME.sort_key("bbb")


def test_valid_keys_never_start_with_the_fallback_prefix() -> None:
    assert all(SCHEME.sort_key(raw)[0] != 0 for raw in SAMPLES)
    assert all(encode_number(digits)[0] != 0 for digits in ["0", "7", "10", "9" * 255])


def test_encode_number() -> None:
    assert encode_number("0") == encode_number("000") == b"\x010"
    assert encode_number("9") < encode_number("10") < encode_number("100")
    with pytest.raises(ValueError):
        encode_number("1" * 256)


def test_version_too_large_to_encode_is_invalid() -> None:
    raw = "1" * 300
    with pytest.raises(InvalidVersion):
        SCHEME.validate(raw)
    assert SCHEME.sort_key(raw) == fallback_sort_key(raw)


@pytest.mark.parametrize("raw", SAMPLES + ["2.5.1", "1.0.0rc1", "1.0-1", "1.0_post1", "V1.0", "1.0.post"])
def test_validate_accepts_what_packaging_accepts(raw: str) -> None:
    SCHEME.validate(raw)


@pytest.mark.parametrize("raw", ["", "not-a-version", "1..0", "1.0.x", "latest", "1.0+", "1.0+bad_local!"])
def test_validate_rejects_what_packaging_rejects(raw: str) -> None:
    with pytest.raises(InvalidVersion):
        SCHEME.validate(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.0", False),
        ("1.0.post1", False),
        ("1.0+cu121", False),
        ("1.0a1", True),
        ("1.0b2", True),
        ("1.0rc1", True),
        ("1.0.dev1", True),
        ("1.0.post1.dev1", True),
        ("not-a-version", False),
        ("", False),
    ],
)
def test_is_prerelease(raw: str, expected: bool) -> None:
    assert SCHEME.is_prerelease(raw) is expected


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("3.1.1", "3.1.2", "patch"),
        ("3.1.1", "3.2.0", "minor"),
        ("3.1.1", "4.0.0", "major"),
        ("3.1.1", "3.2.0rc1", "pre"),
        ("3.1.1", "3.2.0.dev1", "pre"),
        ("3.1.1", "3.0.9", "other"),
        ("2", "2.1", "minor"),
        ("2.8.0", "2.8.0+cpu", "other"),
        ("not-a-version", "1.0", "other"),
        (None, "1.0", None),
        ("1.0", "1!0.5", "major"),
        ("1.0", "1.0", "other"),
    ],
)
def test_bump(previous: str | None, current: str, expected: str | None) -> None:
    assert SCHEME.bump(previous, current) == expected


@pytest.mark.parametrize(
    ("previous", "current"),
    [(a, b) for a in [None, "3.1.1", "2", "2.8.0", "not-a-version", "1.0"] for b in ["1.0", "3.2.0rc1", "bad", "2.8.0+cpu", "4.0"]],
)
def test_bump_matches_classify_bump(previous: str | None, current: str) -> None:
    from app.fetcher.events import classify_bump

    assert SCHEME.bump(previous, current) == classify_bump(previous, current)


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        ("*", "1.0", True),
        ("", "1.0", True),
        ("  ", "1.0a1", True),
        ("*", "not-a-version", False),
        ("", "not-a-version", False),
        (">=3.0,<4", "3.5", True),
        (">=3.0,<4", "4.0", False),
        (">=3.0,<4", "2.9", False),
        (">=3.0,<4", "3.5rc1", True),
        (">=3.0", "3.0rc1", False),
        (">=3.0rc1", "3.0rc2", True),
        ("==2.5.1", "2.5.1", True),
        ("==2.5.1", "2.5.1+cu121", True),
        ("==2.5.1+cu121", "2.5.1", False),
        ("==2.5.*", "2.5.9", True),
        ("==2.5.*", "2.6.0", False),
        ("~=1.4.2", "1.4.9", True),
        ("~=1.4.2", "1.5.0", False),
        ("!=1.5", "1.5", False),
        (">=1.0", "garbage", False),
    ],
)
def test_constraint_contains(spec: str, version: str, expected: bool) -> None:
    assert SCHEME.constraint(spec).contains(version) is expected


def test_constraint_raw_is_preserved() -> None:
    assert SCHEME.constraint(">=3.0,<4").raw() == ">=3.0,<4"
    assert SCHEME.constraint("*").raw() == "*"
    assert SCHEME.constraint("").raw() == ""


def test_constraint_rejects_invalid_specifier() -> None:
    with pytest.raises(InvalidVersion):
        SCHEME.constraint(">>1.0")
