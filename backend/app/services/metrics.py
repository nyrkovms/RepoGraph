from collections.abc import Iterable
from datetime import datetime
from time import perf_counter

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

API_LATENCY_SECONDS = Histogram(
    "packagehub_api_latency_seconds",
    "HTTP request latency",
    ["method", "route", "status_code"],
)

RESOLVE_LATENCY_SECONDS = Histogram(
    "packagehub_resolve_latency_seconds",
    "Dependency resolve latency",
)

RESOLVE_CACHE_HITS = Counter(
    "packagehub_resolve_cache_hits_total",
    "Total resolve cache hits",
)

RESOLVE_CACHE_MISSES = Counter(
    "packagehub_resolve_cache_misses_total",
    "Total resolve cache misses",
)

SYNC_DURATION_SECONDS = Histogram(
    "packagehub_sync_duration_seconds",
    "Sync task duration",
    ["operation"],
)

SYNC_LAST_DURATION_SECONDS = Gauge(
    "packagehub_sync_last_duration_seconds",
    "Last known sync duration from DB cursor data",
    ["operation"],
)

SYNC_LAST_STATUS = Gauge(
    "packagehub_sync_last_status",
    "Last known sync status from DB (idle=0,running=1,failed=2)",
)

DAILY_UPDATE_DURATION_SECONDS = Histogram(
    "packagehub_daily_update_duration_seconds",
    "Daily update run duration",
    ["ecosystem"],
)

DAILY_UPDATE_PACKAGES = Gauge(
    "packagehub_daily_update_packages",
    "Packages by result in the last daily update run",
    ["ecosystem", "result"],
)

DAILY_UPDATE_LAST_SUCCESS_TIMESTAMP = Gauge(
    "packagehub_daily_update_last_success_timestamp",
    "Unix timestamp of the last successful (or partial) daily update run",
    ["ecosystem"],
)

PACKAGE_EVENTS_TOTAL = Counter(
    "packagehub_package_events_total",
    "Total package change events recorded",
    ["ecosystem", "kind"],
)

# Per source, from sync_states (SPEC-MULTI-ECOSYSTEM.md §6.6: breaker state and detector cursors).
SOURCE_LABELS = ["ecosystem", "registry"]

SOURCE_DEGRADED = Gauge("packagehub_source_degraded", "1 while the source is behind its circuit breaker", SOURCE_LABELS)

SOURCE_CONSECUTIVE_FAILURES = Gauge(
    "packagehub_source_consecutive_failures", "Failed requests to the source in a row", SOURCE_LABELS
)

SOURCE_LAST_SUCCESS_TIMESTAMP = Gauge(
    "packagehub_source_last_success_timestamp", "Unix timestamp of the source's last answer", SOURCE_LABELS
)

SOURCE_CURSOR = Gauge(
    "packagehub_source_cursor", "Numeric values of the source's change-detector cursor", [*SOURCE_LABELS, "key"]
)


class observe_api_latency:
    def __init__(self, method: str, route: str):
        self.method = method
        self.route = route
        self.start = 0.0

    def __enter__(self):
        self.start = perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        # No status code in context manager; use explicit observe() for middleware.
        return False


def observe_http_request(method: str, route: str, status_code: int, elapsed_seconds: float) -> None:
    API_LATENCY_SECONDS.labels(method=method, route=route, status_code=str(status_code)).observe(elapsed_seconds)


def observe_resolve_latency(elapsed_seconds: float) -> None:
    RESOLVE_LATENCY_SECONDS.observe(elapsed_seconds)


def inc_resolve_cache_hit() -> None:
    RESOLVE_CACHE_HITS.inc()


def inc_resolve_cache_miss() -> None:
    RESOLVE_CACHE_MISSES.inc()


def observe_sync_duration(operation: str, elapsed_seconds: float) -> None:
    SYNC_DURATION_SECONDS.labels(operation=operation).observe(elapsed_seconds)


def update_sync_gauges(states: Iterable) -> None:
    status_map = {"idle": 0, "running": 1, "failed": 2}
    max_status = 0

    for state in states:
        status_value = status_map.get(getattr(state.status, "value", str(state.status)), 0)
        max_status = max(max_status, status_value)

        cursor_data = state.cursor_data or {}
        operation = cursor_data.get("last_operation")
        duration_ms = cursor_data.get("last_duration_ms")
        if operation and duration_ms is not None:
            try:
                SYNC_LAST_DURATION_SECONDS.labels(operation=operation).set(float(duration_ms) / 1000.0)
            except Exception:  # noqa: BLE001
                continue

    SYNC_LAST_STATUS.set(max_status)


def update_source_gauges(sources: Iterable[tuple[str, str, object]], now: datetime) -> None:
    """`sources`: (ecosystem, registry slug, sync_states row) for every source that has a state."""
    for ecosystem, slug, state in sources:
        labels = {"ecosystem": ecosystem, "registry": slug}
        degraded_until = getattr(state, "degraded_until", None)
        SOURCE_DEGRADED.labels(**labels).set(1 if degraded_until is not None and degraded_until > now else 0)
        SOURCE_CONSECUTIVE_FAILURES.labels(**labels).set(getattr(state, "consecutive_failures", 0) or 0)
        last_success = getattr(state, "last_success_at", None)
        if last_success is not None:
            SOURCE_LAST_SUCCESS_TIMESTAMP.labels(**labels).set(last_success.timestamp())
        for key, value in (getattr(state, "cursor_data", None) or {}).items():
            # Only numbers make a gauge (PyPI index_serial, npm seq); ETags and timestamps as text are skipped.
            if isinstance(value, int | float) and not isinstance(value, bool):
                SOURCE_CURSOR.labels(**labels, key=key).set(value)


def record_daily_update_run(
    status: str,
    duration_seconds: float,
    updated: int,
    failed: int,
    unavailable: int,
    finished_at_ts: float,
    ecosystem: str = "pypi",
) -> None:
    DAILY_UPDATE_DURATION_SECONDS.labels(ecosystem=ecosystem).observe(duration_seconds)
    DAILY_UPDATE_PACKAGES.labels(ecosystem=ecosystem, result="updated").set(updated)
    DAILY_UPDATE_PACKAGES.labels(ecosystem=ecosystem, result="failed").set(failed)
    DAILY_UPDATE_PACKAGES.labels(ecosystem=ecosystem, result="unavailable").set(unavailable)
    if status in {"success", "partial"}:
        DAILY_UPDATE_LAST_SUCCESS_TIMESTAMP.labels(ecosystem=ecosystem).set(finished_at_ts)


def inc_package_events(kind: str, count: int = 1, ecosystem: str = "pypi") -> None:
    if count > 0:
        PACKAGE_EVENTS_TOTAL.labels(ecosystem=ecosystem, kind=kind).inc(count)


def render_metrics() -> tuple[bytes, str]:
    payload = generate_latest()
    return payload, CONTENT_TYPE_LATEST
