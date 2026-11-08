from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from prometheus_client import generate_latest

from app.models.enums import Ecosystem
from app.services.metrics import inc_package_events, record_daily_update_run, update_run_gauges, update_source_gauges

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


def _run(operation: str, ecosystem: Ecosystem | None, status: str, started: datetime,
         finished: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(operation=operation, ecosystem=ecosystem, status=status, started_at=started,
                           finished_at=finished)


def test_run_gauges_come_from_the_runs_journal() -> None:
    """#71: a finished run gives a duration sample, and a failed or stale run shows as failed."""
    guard = timedelta(hours=6)
    done = _run("m-daily", Ecosystem.npm, "partial", NOW - timedelta(minutes=10), NOW - timedelta(minutes=7))
    failed = _run("m-daily", Ecosystem.pypi, "failed", NOW - timedelta(hours=1), NOW - timedelta(minutes=59))
    going = _run("m-seed", None, "running", NOW - timedelta(minutes=5))
    dead = _run("m-probe", Ecosystem.go, "running", NOW - timedelta(hours=7))
    update_run_gauges([done, failed, going, dead], [done, failed], NOW, guard)
    body = _metrics()
    assert 'packagehub_sync_last_duration_seconds{ecosystem="npm",operation="m-daily"} 180.0' in body
    assert 'packagehub_sync_last_duration_seconds{ecosystem="pypi",operation="m-daily"} 60.0' in body
    assert 'packagehub_sync_last_status{ecosystem="npm",operation="m-daily"} 0.0' in body
    assert 'packagehub_sync_last_status{ecosystem="pypi",operation="m-daily"} 2.0' in body
    assert 'packagehub_sync_last_status{ecosystem="all",operation="m-seed"} 1.0' in body
    assert 'packagehub_sync_last_status{ecosystem="go",operation="m-probe"} 2.0' in body
    assert 'packagehub_sync_last_duration_seconds{ecosystem="all",operation="m-seed"}' not in body  # still running
