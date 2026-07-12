import random
import ssl

import httpx


def is_retryable_upstream_error(exc: Exception) -> bool:
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        return status == 429 or 500 <= status <= 599
    return False


def compute_backoff_delay_seconds(
    attempt: int,
    base_seconds: float,
    max_seconds: float,
    jitter_seconds: float,
) -> float:
    # attempt is 1-based
    exponent = max(0, attempt - 1)
    base_delay = base_seconds * (2**exponent)
    jitter = random.uniform(0.0, max(0.0, jitter_seconds))
    return min(max_seconds, base_delay + jitter)
