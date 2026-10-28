import asyncio
import logging

import httpx

from app.config import get_settings
from app.normalizers.names import normalize_package_name
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error
from app.services.http import make_client

logger = logging.getLogger(__name__)

SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"


class PyPISimpleIndex:
    """Reads project serials from the PEP 691 Simple API to detect which packages changed."""

    def __init__(self, timeout_seconds: float = 120.0) -> None:
        settings = get_settings()
        self.base_url = settings.sync_pypi_simple_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = settings.sync_http_max_retries
        self.retry_base_seconds = settings.sync_http_retry_base_seconds
        self.retry_max_seconds = settings.sync_http_retry_max_seconds
        self.retry_jitter_seconds = settings.sync_http_retry_jitter_seconds

    async def fetch_all_serials(self) -> tuple[int | None, dict[str, int]]:
        """Return the index serial and normalized project name -> _last-serial for all of PyPI."""
        payload = await self._get_json(f"{self.base_url}/")
        serials: dict[str, int] = {}
        for project in payload.get("projects") or []:
            name = project.get("name")
            serial = project.get("_last-serial")
            if name and isinstance(serial, int):
                serials[normalize_package_name(name)] = serial
        return _meta_serial(payload), serials

    async def fetch_project_serial(self, name: str) -> int | None:
        """Return the project's _last-serial, or None when PyPI no longer lists it."""
        try:
            payload = await self._get_json(f"{self.base_url}/{normalize_package_name(name)}/")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return _meta_serial(payload)

    async def _get_json(self, url: str) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with make_client(timeout=self.timeout_seconds) as client:
                    response = await client.get(url, headers={"Accept": SIMPLE_JSON_ACCEPT})
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                not_found = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
                if not_found or not is_retryable_upstream_error(exc) or attempt > self.max_retries:
                    raise
                delay = compute_backoff_delay_seconds(
                    attempt=attempt,
                    base_seconds=self.retry_base_seconds,
                    max_seconds=self.retry_max_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                logger.warning("Simple index retry: url=%s attempt=%s delay=%.2fs error=%s", url, attempt, delay, exc.__class__.__name__)
                await asyncio.sleep(delay)


def _meta_serial(payload: dict) -> int | None:
    serial = (payload.get("meta") or {}).get("_last-serial")
    return serial if isinstance(serial, int) else None
