import ssl

import httpx
from sqlalchemy.exc import DBAPIError

from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error


def test_retry_policy_marks_ssl_error_as_retryable() -> None:
    exc = ssl.SSLError("record layer failure")
    assert is_retryable_upstream_error(exc) is True


def test_retry_policy_marks_http_429_as_retryable() -> None:
    request = httpx.Request("GET", "https://pypi.org/pypi/flask/json")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("too many requests", request=request, response=response)
    assert is_retryable_upstream_error(exc) is True


def test_retry_policy_marks_http_404_as_non_retryable() -> None:
    request = httpx.Request("GET", "https://pypi.org/pypi/missing/json")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert is_retryable_upstream_error(exc) is False


def test_backoff_delay_is_positive_and_capped() -> None:
    delay = compute_backoff_delay_seconds(
        attempt=10,
        base_seconds=0.5,
        max_seconds=1.0,
        jitter_seconds=0.0,
    )
    assert delay == 1.0


def _database_error(sqlstate: str) -> DBAPIError:
    orig = Exception("database error")
    orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return DBAPIError("UPDATE sync_states", {}, orig)


def test_retry_policy_marks_an_aborted_transaction_as_retryable() -> None:
    """Regression (#78): a deadlock between two writes was recorded as a failure nothing would retry."""
    assert is_retryable_upstream_error(_database_error("40P01")) is True
    assert is_retryable_upstream_error(_database_error("40001")) is True


def test_retry_policy_marks_other_database_errors_as_non_retryable() -> None:
    assert is_retryable_upstream_error(_database_error("23505")) is False
