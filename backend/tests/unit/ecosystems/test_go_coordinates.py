"""Go coordinates (WP H2): module paths as written (CheckPath rules), proxy escaping, golang purls."""

import pytest

from app.ecosystems.base import PackageRef
from app.ecosystems.go.coordinates import (
    GoCoordinates,
    escape_path,
    escape_version,
    unescape_path,
    validate_path,
)

COORDINATES = GoCoordinates()


def _ref(path: str) -> PackageRef:
    return PackageRef(ecosystem="go", namespace="", name=path)


def test_paths_are_kept_as_written() -> None:
    assert COORDINATES.parse("github.com/BurntSushi/toml") == _ref("github.com/BurntSushi/toml")
    assert COORDINATES.parse("  golang.org/x/net\n") == _ref("golang.org/x/net")
    assert COORDINATES.parse("gopkg.in/yaml.v3") == _ref("gopkg.in/yaml.v3")
    assert COORDINATES.parse("github.com/redis/go-redis/v9") != COORDINATES.parse("github.com/redis/go-redis")
    assert COORDINATES.parse("github.com/BurntSushi/toml") != COORDINATES.parse("github.com/burntsushi/toml")
    assert COORDINATES.display(_ref("rsc.io/quote/v3")) == "rsc.io/quote/v3"


@pytest.mark.parametrize("path", [
    "github.com/BurntSushi/toml", "rsc.io/quote", "rsc.io/quote/v3", "golang.org/x/net", "gopkg.in/yaml.v3",
    "gopkg.in/check.v1", "gopkg.in/yaml.v3-unstable", "gopkg.in/natefinch/lumberjack.v2", "example.com",
    "k8s.io/api", "go.uber.org/zap", "my-host.example.com/a_b/c~d/e.f", "example.com/v2.1", "example.com/v", "a.b/v10",
    "example.com/console", "example.com/com10", "example.com/foo.v2",
])
def test_valid_paths(path: str) -> None:
    validate_path(path)


@pytest.mark.parametrize("path", [
    "", "  ", "foo", "foo/bar",                                           # no dot in the first element
    "github.com/über", "gíthub.com/x",                                    # ASCII only
    "GitHub.com/x/y", "Example.com",                                      # first element is lowercase
    "ex_ample.com/x", "ex ample.com/x", "-example.com/x", ".example.com/x",
    "example.com//x", "/example.com/x", "example.com/x/",                 # empty elements, trailing slash
    "example.com/./x", "example.com/../x", "example.com/.x", "example.com/x.", "example.com/a/..",
    "example.com/a b", "example.com/a:b", "example.com/a@b", "example.com/a+b", "example.com/a!b",
    "example.com/CON", "example.com/con", "example.com/Nul", "example.com/aux.txt", "example.com/COM1",
    "example.com/lpt9.go", "example.com/PRN.x.y",                         # Windows names, also with an extension
    "example.com/v0", "example.com/v1", "example.com/v02", "example.com/x/v01", "example.com/v00",
    "gopkg.in/yaml", "gopkg.in/yaml.v", "gopkg.in/yaml.v02", "gopkg.in/yaml/v3", "gopkg.in/.v3", "gopkg.in/yaml.v3x",
])
def test_invalid_paths(path: str) -> None:
    with pytest.raises(ValueError):
        validate_path(path)
    with pytest.raises(ValueError):
        COORDINATES.parse(path)


@pytest.mark.parametrize(("path", "escaped"), [
    ("github.com/BurntSushi/toml", "github.com/!burnt!sushi/toml"),
    ("golang.org/x/net", "golang.org/x/net"),
    ("github.com/Azure/azure-sdk-for-go", "github.com/!azure/azure-sdk-for-go"),
    ("github.com/ABC", "github.com/!a!b!c"),
])
def test_path_escaping_round_trips(path: str, escaped: str) -> None:
    assert escape_path(path) == escaped
    assert unescape_path(escaped) == path


def test_versions_are_escaped_like_paths() -> None:
    assert escape_version("v1.2.3") == "v1.2.3"
    assert escape_version("v1.0.0-RC1") == "v1.0.0-!r!c1"
    assert escape_version("v0.0.0-20200101000000-abcdef123456") == "v0.0.0-20200101000000-abcdef123456"


@pytest.mark.parametrize("escaped", ["github.com/BurntSushi/toml", "a!", "a!!b", "a!Bc", "a!1", "a!/b", "!"])
def test_unescape_rejects(escaped: str) -> None:
    with pytest.raises(ValueError):
        unescape_path(escaped)


def test_escape_rejects_non_ascii() -> None:
    with pytest.raises(ValueError):
        escape_path("example.com/über")


@pytest.mark.parametrize(("purl", "path", "version"), [
    ("pkg:golang/golang.org/x/net", "golang.org/x/net", None),
    ("pkg:golang/rsc.io/quote", "rsc.io/quote", None),
    ("pkg:golang/github.com/redis/go-redis/v9", "github.com/redis/go-redis/v9", None),
    ("pkg:golang/github.com/BurntSushi/toml@v1.4.0", "github.com/BurntSushi/toml", "v1.4.0"),
    ("pkg:golang/gopkg.in/yaml.v3@v3.0.1", "gopkg.in/yaml.v3", "v3.0.1"),
    ("pkg:golang/golang.org/x/net@v0.0.0-20210405180319-a5a99cb37ef4", "golang.org/x/net",
     "v0.0.0-20210405180319-a5a99cb37ef4"),
    ("pkg:golang/github.com/tools/godep@v80%2Bincompatible", "github.com/tools/godep", "v80+incompatible"),
    ("pkg:golang/rsc.io/quote/v3@v3.1.0", "rsc.io/quote/v3", "v3.1.0"),
])
def test_purls_in_both_directions(purl: str, path: str, version: str | None) -> None:
    assert COORDINATES.from_purl(purl) == (_ref(path), version)
    assert COORDINATES.to_purl(_ref(path), version) == purl


def test_purl_namespace_is_the_path_without_its_last_element() -> None:
    assert COORDINATES.to_purl(_ref("github.com/redis/go-redis/v9")) == "pkg:golang/github.com/redis/go-redis/v9"
    assert COORDINATES.from_purl("pkg:golang/rsc.io/quote")[0].name == "rsc.io/quote"


@pytest.mark.parametrize("purl", [
    "pkg:npm/golang.org/x/net", "pkg:cargo/serde",
    "pkg:golang/net",                                                     # no namespace
    "pkg:golang/golang.org/x/Net%20x", "pkg:golang/foo/bar",              # not module paths
    "pkg:golang/github.com/foo/v1", "pkg:golang/GitHub.com/x",
])
def test_purls_that_are_not_modules(purl: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.from_purl(purl)


def test_a_one_element_path_has_no_purl() -> None:
    assert COORDINATES.parse("example.com") == _ref("example.com")
    with pytest.raises(ValueError):
        COORDINATES.to_purl(_ref("example.com"))
