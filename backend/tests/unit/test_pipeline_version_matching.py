from app.fetcher.pipeline import plugin_for, select_best_version

PYPI = plugin_for("pypi")


def test_select_best_version_falls_back_to_prerelease_when_no_stable_match() -> None:
    versions = [("0.49b0", "v1"), ("0.50b0", "v2"), ("0.60b1", "v3")]

    assert select_best_version(PYPI, versions, "==0.50b0") == "v2"


def test_select_best_version_matches_local_versions() -> None:
    versions = [("2.8.0+cpu", "cpu"), ("2.8.0", "base")]

    assert select_best_version(PYPI, versions, "==2.8.0+cpu") == "cpu"


def test_select_best_version_prefers_stable_and_skips_invalid() -> None:
    versions = [("1.0", "a"), ("2.0rc1", "b"), ("1.5", "c"), ("not a version", "d")]

    assert select_best_version(PYPI, versions, ">=1") == "c"
    assert select_best_version(PYPI, versions, "not a specifier") == "c"
    assert select_best_version(PYPI, versions, ">=3") is None


def test_select_best_version_takes_a_maven_soft_version_as_named() -> None:
    maven = plugin_for("maven")
    versions = [("6.1.13", "a"), ("6.1.14", "b"), ("6.2.0", "c")]

    assert select_best_version(maven, versions, "6.1.14") == "b"          # admits any version, names one
    assert select_best_version(maven, versions, "6.0.0") == "c"           # not published here: the highest
    assert select_best_version(maven, versions, "[6.1,6.2)") == "b"
