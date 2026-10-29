"""Packages addressed by purl in seed, fetch and retry; the key mapping of sync_failures."""

import pytest

from app.ecosystems.base import PackageRef
from app.services import sync_failures
from app.tasks.fetch_packages import ref_from_target

FLASK = PackageRef("pypi", "", "flask")


@pytest.mark.parametrize(
    "raw",
    ["flask", "Flask", " flask ", "pkg:pypi/flask", "pkg:pypi/Flask", "pkg:pypi/flask@3.0.0"],
)
def test_bare_name_is_a_pypi_name_and_purl_is_a_purl(raw: str) -> None:
    assert ref_from_target(raw) == FLASK


def test_underscores_and_case_are_normalized_like_pep_503() -> None:
    assert ref_from_target("Typing_Extensions") == PackageRef("pypi", "", "typing-extensions")
    assert ref_from_target("pkg:pypi/Typing_Extensions") == PackageRef("pypi", "", "typing-extensions")


@pytest.mark.parametrize("raw", ["", "  ", "pkg:", "pkg:pypi/", "pkg:pypi/ns/flask", "pkg:cobol/flask"])
def test_bad_targets_raise_value_error(raw: str) -> None:
    with pytest.raises(ValueError):
        ref_from_target(raw)


def test_failure_key_is_the_purl() -> None:
    assert sync_failures._purl(FLASK) == "pkg:pypi/flask"
    assert sync_failures._purl(PackageRef("pypi", "", "typing-extensions")) == "pkg:pypi/typing-extensions"
