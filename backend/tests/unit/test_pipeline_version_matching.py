from types import SimpleNamespace

from app.fetcher.pipeline import _select_best_version_id


def test_select_best_version_id_falls_back_to_prerelease_when_no_stable_match() -> None:
    versions = [
        SimpleNamespace(id="v1", version="0.49b0"),
        SimpleNamespace(id="v2", version="0.50b0"),
        SimpleNamespace(id="v3", version="0.60b1"),
    ]

    result = _select_best_version_id(versions, "==0.50b0")

    assert result == "v2"


def test_select_best_version_id_matches_local_versions() -> None:
    versions = [
        SimpleNamespace(id="cpu", version="2.8.0+cpu"),
        SimpleNamespace(id="base", version="2.8.0"),
    ]

    result = _select_best_version_id(versions, "==2.8.0+cpu")

    assert result == "cpu"
