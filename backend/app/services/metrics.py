from collections.abc import Iterable
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

PROBE_DURATION_SECONDS = Histogram(
    "packagehub_probe_duration_seconds",
    "Distribution probe duration",
    ["registry", "outcome"],
)

PROBE_OUTCOMES = Counter(
    "packagehub_probe_outcomes_total",
    "Probe outcomes (probed/unusable/skipped/failed)",
    ["registry", "outcome"],
)

ENUMERATION_SYNCED = Counter(
    "packagehub_enumeration_synced_total",
    "Packages discovered and synced by the enumeration loop",
    ["registry"],
)

FETCH_ERRORS = Counter(
    "packagehub_fetch_errors_total",
    "Fetcher errors by class",
    ["registry", "error_class"],
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


def observe_probe(registry: str, outcome: str, elapsed_seconds: float) -> None:
    PROBE_DURATION_SECONDS.labels(registry=registry, outcome=outcome).observe(elapsed_seconds)
    PROBE_OUTCOMES.labels(registry=registry, outcome=outcome).inc()


def inc_enumeration_synced(registry: str, count: int = 1) -> None:
    ENUMERATION_SYNCED.labels(registry=registry).inc(count)


def inc_fetch_error(registry: str, error_class: str) -> None:
    FETCH_ERRORS.labels(registry=registry, error_class=error_class).inc()


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


def render_metrics() -> tuple[bytes, str]:
    payload = generate_latest()
    return payload, CONTENT_TYPE_LATEST
