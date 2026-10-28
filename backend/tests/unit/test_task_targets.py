"""Packages addressed by purl in seed, fetch and retry; the key mapping of sync_failures."""

import pytest
from sqlalchemy.dialects import postgresql

from app.ecosystems.base import PackageRef
from app.services import sync_failures
from app.tasks.fetch_packages import ref_from_target
from app.tasks.retry_failed import failure_ref

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


def test_failure_row_without_package_ref_counts_as_pypi() -> None:
    assert failure_ref(None, "flask") == FLASK
    assert failure_ref("", "flask") == FLASK
    assert failure_ref("pkg:pypi/flask", "ignored") == FLASK


def test_failure_key_is_the_purl_and_the_legacy_name() -> None:
    assert sync_failures._key(FLASK) == ("pkg:pypi/flask", "flask")


def test_other_ecosystems_get_the_purl_as_legacy_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # The old unique key (operation, package_name) must not make npm `flask` collide with PyPI `flask`.
    class _Coordinates:
        def to_purl(self, ref: PackageRef) -> str:
            return f"pkg:npm/{ref.name}"

    monkeypatch.setattr(sync_failures, "_plugin", lambda ecosystem: type("P", (), {"coordinates": _Coordinates()})())
    assert sync_failures._key(PackageRef("npm", "", "flask")) == ("pkg:npm/flask", "pkg:npm/flask")

    long = PackageRef("npm", "", "x" * 400)
    purl, name = sync_failures._key(long)
    assert purl == "pkg:npm/" + "x" * 400 and len(name) == 255
    assert name != sync_failures._key(PackageRef("npm", "", "x" * 401))[1]


def _where(ref: PackageRef) -> str:
    return str(sync_failures._matches(ref).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_pypi_lookup_matches_rows_written_before_package_ref() -> None:
    where = _where(FLASK)
    assert "package_ref = 'pkg:pypi/flask'" in where
    assert "package_ref IS NULL AND sync_failures.package_name = 'flask'" in where


def test_other_ecosystems_are_looked_up_by_purl_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Coordinates:
        def to_purl(self, ref: PackageRef) -> str:
            return f"pkg:npm/{ref.name}"

    monkeypatch.setattr(sync_failures, "_plugin", lambda ecosystem: type("P", (), {"coordinates": _Coordinates()})())
    where = _where(PackageRef("npm", "", "left-pad"))
    assert "package_ref = 'pkg:npm/left-pad'" in where and "IS NULL" not in where


def test_legacy_package_name_is_a_normalized_pypi_ref() -> None:
    assert sync_failures._resolve_ref(None, "Typing_Extensions") == PackageRef("pypi", "", "typing-extensions")
    with pytest.raises(TypeError):
        sync_failures._resolve_ref(None, None)
