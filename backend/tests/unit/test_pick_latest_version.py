from app.connectors.pypi import _pick_latest_version


def test_numeric_ordering_not_lexicographic():
    assert _pick_latest_version({"9.0": [], "10.0": []}) == "10.0"


def test_prefers_stable_over_prerelease():
    assert _pick_latest_version({"1.0.0": [], "2.0.0rc1": []}) == "1.0.0"


def test_prerelease_only_falls_back_to_prerelease():
    assert _pick_latest_version({"2.0.0rc1": [], "2.0.0rc2": []}) == "2.0.0rc2"


def test_empty_releases():
    assert _pick_latest_version({}) == "0"


def test_unparseable_versions_fall_back_to_lexicographic():
    assert _pick_latest_version({"not-a-version": [], "also-bad": []}) == "not-a-version"


def test_local_version_labels_supported():
    assert _pick_latest_version({"2.1.0": [], "2.1.0+cu121": []}) == "2.1.0+cu121"
