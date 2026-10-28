from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from prometheus_client import generate_latest

from app.services.metrics import inc_package_events, record_daily_update_run, update_source_gauges

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def _metrics() -> str:
    return generate_latest().decode()


def test_source_gauges_carry_ecosystem_and_registry() -> None:
    state = SimpleNamespace(degraded_until=NOW + timedelta(minutes=5), consecutive_failures=5,
                            last_success_at=NOW - timedelta(hours=1),
                            cursor_data={"index_serial": 42, "etag": '"abc"', "full": True})
    update_source_gauges([("pypi", "metrics-test", state)], NOW)
    body = _metrics()
    labels = 'ecosystem="pypi",registry="metrics-test"'
    assert f"packagehub_source_degraded{{{labels}}} 1.0" in body
    assert f"packagehub_source_consecutive_failures{{{labels}}} 5.0" in body
    assert 'packagehub_source_cursor{ecosystem="pypi",key="index_serial",registry="metrics-test"} 42.0' in body
    assert 'key="etag"' not in body and 'key="full"' not in body   # only numbers make a gauge


def test_daily_update_and_event_metrics_have_an_ecosystem_label() -> None:
    record_daily_update_run("success", 1.5, updated=3, failed=0, unavailable=1, finished_at_ts=NOW.timestamp(),
                            ecosystem="pypi")
    inc_package_events("release_added", 2, ecosystem="pypi")
    body = _metrics()
    assert 'packagehub_daily_update_packages{ecosystem="pypi",result="updated"} 3.0' in body
    assert 'packagehub_package_events_total{ecosystem="pypi",kind="release_added"}' in body
