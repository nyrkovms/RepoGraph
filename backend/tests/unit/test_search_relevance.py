"""The rank of a search hit and the purl resolution of the search box."""

import pytest

from app.api.v1.packages import PackageSort, _query_forms, _ref_from_purl, _relevance_rank_of
from app.ecosystems.base import PackageRef


@pytest.mark.parametrize(
    ("name", "q", "rank"),
    [("django", "django", 0), ("django", "DJANGO", 0), ("django-rest", "django", 1), ("djangorestframework", "Dja", 1),
     ("flask-django", "django", 2), ("django", "django-", 2)],
)
def test_rank(name: str, q: str, rank: int) -> None:
    assert _relevance_rank_of(name, q) == rank


@pytest.mark.parametrize(
    ("name", "q", "rank"),
    [("typing-extensions", "typing_extensions", 0), ("typing-extensions", "Typing.Extensions", 0),
     ("typing-extensions-extra", "typing_extensions", 1), ("x-typing-extensions", "typing_extensions", 2)],
)
def test_rank_reads_underscore_and_dot_as_a_hyphen(name: str, q: str, rank: int) -> None:
    assert _relevance_rank_of(name, q) == rank


def test_query_forms_are_the_typed_one_and_the_hyphenated_one() -> None:
    assert _query_forms("Typing_Extensions") == ["typing_extensions", "typing-extensions"]
    assert _query_forms("typing-extensions") == ["typing-extensions"]


def test_relevance_is_a_sort() -> None:
    assert PackageSort("relevance") is PackageSort.relevance


def test_purl_resolves_with_the_plugin_and_ignores_the_version() -> None:
    assert _ref_from_purl("pkg:pypi/Django@5.1") == PackageRef("pypi", "", "django")
    assert _ref_from_purl("pkg:npm/%40angular/core") == PackageRef("npm", "angular", "core")


def test_rpm_purl_takes_the_namespace_from_the_distro() -> None:
    ref = _ref_from_purl("pkg:rpm/rocky/bash?distro=rocky-9")
    assert ref is not None and ref.name == "bash" and ref.namespace.startswith("rocky")


@pytest.mark.parametrize("raw", ["pkg:", "pkg:npm", "pkg:cobol/x", "pkg:rpm/rocky/bash", "pkg:pypi/", "pkg:::"])
def test_unusable_purl_is_none_not_an_error(raw: str) -> None:
    assert _ref_from_purl(raw) is None
