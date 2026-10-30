"""crates coordinates (WP G2): names as published (Q21), purls in both directions, sparse index paths."""

import pytest

from app.ecosystems.base import PackageRef
from app.ecosystems.crates.coordinates import CratesCoordinates, dependency_ref, index_path

COORDINATES = CratesCoordinates()


def _ref(name: str) -> PackageRef:
    return PackageRef(ecosystem="crates", namespace="", name=name)


def test_names_are_kept_as_published() -> None:
    assert COORDINATES.parse("serde_json") == _ref("serde_json")
    assert COORDINATES.parse("  proc-macro2\n") == _ref("proc-macro2")
    assert COORDINATES.parse("Inflector") == _ref("Inflector")
    assert COORDINATES.parse("serde-json") != COORDINATES.parse("serde_json")   # crates.io holds only one of them
    assert COORDINATES.display(_ref("serde_json")) == "serde_json"
    assert COORDINATES.parse("a" * 64).name == "a" * 64


@pytest.mark.parametrize("raw", ["", "  ", "1password", "_private", "-dash", "serde.json", "über", "a/b", "a" * 65,
                                 "serde json"])
def test_invalid_names(raw: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.parse(raw)


def test_dependency_targets_only_need_the_shape() -> None:
    assert dependency_ref("_internal") == _ref("_internal")
    with pytest.raises(ValueError):
        dependency_ref("a b")


@pytest.mark.parametrize(("purl", "name", "version"), [
    ("pkg:cargo/serde", "serde", None),
    ("pkg:cargo/serde_json@1.0.140", "serde_json", "1.0.140"),
    ("pkg:cargo/Inflector@0.11.4", "Inflector", "0.11.4"),
    ("pkg:cargo/rand@0.9.0-alpha.1", "rand", "0.9.0-alpha.1"),
])
def test_purls_in_both_directions(purl: str, name: str, version: str | None) -> None:
    assert COORDINATES.from_purl(purl) == (_ref(name), version)
    assert COORDINATES.to_purl(_ref(name), version) == purl


@pytest.mark.parametrize("purl", ["pkg:npm/serde", "pkg:cargo/ns/serde", "pkg:cargo/1bad"])
def test_purls_that_are_not_crates(purl: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.from_purl(purl)


@pytest.mark.parametrize(("name", "path"), [
    ("a", "1/a"), ("ab", "2/ab"), ("abc", "3/a/abc"), ("serde", "se/rd/serde"), ("serde_json", "se/rd/serde_json"),
    ("Inflector", "in/fl/inflector"), ("cc", "2/cc"), ("syn", "3/s/syn"),
])
def test_index_paths(name: str, path: str) -> None:
    assert index_path(name) == path
