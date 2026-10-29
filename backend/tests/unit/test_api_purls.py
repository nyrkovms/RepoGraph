import pytest

from app.api.v1.admin import sync_summary
from app.models.run import Run
from app.services import purls


def test_purl_through_the_plugin() -> None:
    assert purls.package_purl("pypi", "", "flask") == "pkg:pypi/flask"
    assert purls.package_purl("pypi", None, "flask") == "pkg:pypi/flask"


def test_purl_without_a_plugin_uses_the_generic_grammar(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_plugin(ecosystem: str):
        raise purls.UnknownEcosystem(ecosystem)

    monkeypatch.setattr(purls, "get_plugin", no_plugin)
    assert purls.package_purl("npm", "@corp", "ui") == "pkg:npm/%40corp/ui"
    assert purls.package_purl("npm", "", "left-pad") == "pkg:npm/left-pad"
    assert purls.package_purl("go", None, "x") == "pkg:golang/x"
    assert purls.package_purl("crates", "", "serde") == "pkg:cargo/serde"


def _run(operation: str, status: str, **fields) -> Run:
    return Run(operation=operation, status=status, **fields)


def test_sync_summary_without_runs() -> None:
    assert sync_summary(None) == {
        "status": "idle", "sync_type": "incremental", "packages_synced": 0, "packages_total": 0, "last_error": None}


@pytest.mark.parametrize(
    ("operation", "status", "expected_status", "expected_type"),
    [
        ("seed", "running", "running", "full"),
        ("daily_update", "running", "running", "incremental"),
        ("daily_update", "failed", "failed", "incremental"),
        ("seed", "failed", "failed", "full"),
        ("daily_update", "success", "idle", "incremental"),
        ("daily_update", "partial", "idle", "incremental"),
        ("seed", "success", "idle", "full"),
    ],
)
def test_sync_summary_maps_the_latest_run(operation, status, expected_status, expected_type) -> None:
    run = _run(operation, status, packages_checked=7, packages_total=9, error="boom")
    assert sync_summary(run) == {
        "status": expected_status,
        "sync_type": expected_type,
        "packages_synced": 7,
        "packages_total": 9,
        "last_error": "boom",
    }
