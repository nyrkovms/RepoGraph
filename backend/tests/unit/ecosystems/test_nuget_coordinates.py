"""NuGet coordinates (WP I2): case-insensitive ids, PackageIdValidator rules, purls in both directions."""

import pytest

from app.ecosystems.base import PackageRef
from app.ecosystems.nuget.coordinates import MAX_ID_LENGTH, NuGetCoordinates, registration_path, validate_id

COORDINATES = NuGetCoordinates()


def _ref(name: str) -> PackageRef:
    return PackageRef(ecosystem="nuget", namespace="", name=name)


def test_ids_are_lowercased() -> None:
    assert COORDINATES.parse("Newtonsoft.Json") == _ref("newtonsoft.json")
    assert COORDINATES.parse("  Microsoft.Extensions.Logging\n") == _ref("microsoft.extensions.logging")
    assert COORDINATES.parse("NEWTONSOFT.JSON") == COORDINATES.parse("newtonsoft.json")
    assert COORDINATES.display(_ref("newtonsoft.json")) == "newtonsoft.json"


@pytest.mark.parametrize("raw", [
    "a", "7", "_", "_private", "a_b", "a__b", "a.b", "a-b", "a.b-c_d", "Serilog.Sinks.File", "log4net", "System.Text.Json",
    "1.0", "a._b", "a-_b", "Ünïcode.Ids", "a" * MAX_ID_LENGTH,
])
def test_valid_ids(raw: str) -> None:
    validate_id(raw)
    assert COORDINATES.parse(raw).name == raw.lower()


def test_length_limit_is_one_hundred_characters() -> None:
    assert MAX_ID_LENGTH == 100
    with pytest.raises(ValueError, match="longer than 100"):
        COORDINATES.parse("a" * 101)
    with pytest.raises(ValueError, match="longer than 100"):
        COORDINATES.parse("a." * 50 + "b")


@pytest.mark.parametrize("raw", [
    "", "   ",
    ".a", "-a",                 # leading separator
    "a.", "a-",                 # trailing separator
    "a..b", "a--b", "a.-b", "a-.b",   # doubled separators
    ".", "-",
    "a b", "a/b", "a\\b", "a@b", "a:b", "a+b", "a,b", "a*b", "a\tb",
])
def test_invalid_ids(raw: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.parse(raw)


def test_validate_id_does_not_strip_or_accept_a_trailing_newline() -> None:
    with pytest.raises(ValueError):
        validate_id(" a")
    with pytest.raises(ValueError):
        validate_id("a\n")


@pytest.mark.parametrize(("purl", "name", "version"), [
    ("pkg:nuget/newtonsoft.json", "newtonsoft.json", None),
    ("pkg:nuget/newtonsoft.json@13.0.3", "newtonsoft.json", "13.0.3"),
    ("pkg:nuget/serilog@4.0.0-beta.1", "serilog", "4.0.0-beta.1"),
    ("pkg:nuget/foo@1.2.3.4", "foo", "1.2.3.4"),
])
def test_purls_in_both_directions(purl: str, name: str, version: str | None) -> None:
    assert COORDINATES.from_purl(purl) == (_ref(name), version)
    assert COORDINATES.to_purl(_ref(name), version) == purl


def test_purl_accepts_any_case_and_keeps_the_version_as_given() -> None:
    assert COORDINATES.from_purl("pkg:nuget/Newtonsoft.Json@13.0.3-Beta") == (_ref("newtonsoft.json"), "13.0.3-Beta")
    assert COORDINATES.from_purl("pkg:nuget/Newtonsoft.Json") == COORDINATES.from_purl("pkg:nuget/newtonsoft.json")
    assert COORDINATES.to_purl(_ref("newtonsoft.json"), "13.0.3-Beta") == "pkg:nuget/newtonsoft.json@13.0.3-Beta"


@pytest.mark.parametrize("purl", [
    "pkg:npm/newtonsoft.json", "pkg:cargo/serde", "pkg:nuget/ns/newtonsoft.json", "pkg:nuget/bad..id", "pkg:nuget/-a",
])
def test_purls_that_are_not_nuget_ids(purl: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.from_purl(purl)


def test_registration_path_is_the_lowercase_id() -> None:
    assert registration_path(COORDINATES.parse("Newtonsoft.Json")) == "newtonsoft.json"
