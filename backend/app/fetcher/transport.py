"""Registry-aware HTTP transport.

One httpx.AsyncClient per event loop (connection pooling within a task run /
API process), auth headers built from registry.auth_config, and the classifire
invariant: the Authorization header is sent only to the registry's own hosts.
Redirects are followed manually so credentials never leak to a CDN.
"""

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5


class RegistryAuthError(Exception):
    """Upstream rejected our credentials (401/403). Not a not-found, not retryable."""


@dataclass(frozen=True)
class RegistryInfo:
    """Connector-facing view of a registry row (no ORM dependency)."""

    slug: str
    ecosystem: str
    protocol: str
    base_url: str
    api_url: str | None = None
    auth_config: dict | None = None
    registry_type: str = "public"
    enumerate_enabled: bool = False

    @property
    def hosts(self) -> frozenset[str]:
        hosts = {urlparse(self.base_url).netloc}
        if self.api_url:
            hosts.add(urlparse(self.api_url).netloc)
        return frozenset(h for h in hosts if h)


def build_auth_headers(auth_config: dict | None) -> dict[str, str]:
    if not auth_config:
        return {}
    auth_type = auth_config.get("type")
    if auth_type == "bearer" and auth_config.get("token"):
        return {"Authorization": f"Bearer {auth_config['token']}"}
    if auth_type == "basic" and auth_config.get("username") is not None:
        raw = f"{auth_config['username']}:{auth_config.get('password', '')}"
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {}


_clients: dict[int, httpx.AsyncClient] = {}


def get_shared_client(timeout_seconds: float | None = None) -> httpx.AsyncClient:
    """AsyncClient bound to the current event loop.

    httpx clients (like asyncpg pools) are loop-affine: Celery runs each task
    in a fresh asyncio.run() loop, so the client is keyed by loop. Within one
    loop (one task run, or the whole API process) connections are pooled.
    """
    loop = asyncio.get_running_loop()
    for key in [k for k, c in _clients.items() if c.is_closed]:
        _clients.pop(key, None)
    client = _clients.get(id(loop))
    if client is None:
        settings = get_settings()
        client = httpx.AsyncClient(
            timeout=timeout_seconds or settings.sync_http_timeout_seconds,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _clients[id(loop)] = client
    return client


async def aclose_shared_client() -> None:
    """Close the client of the current loop (task teardown / API shutdown)."""
    loop = asyncio.get_running_loop()
    client = _clients.pop(id(loop), None)
    if client is not None and not client.is_closed:
        await client.aclose()


@dataclass
class RegistryClient:
    """Single-attempt HTTP GET with auth scoping, manual redirects and throttling.

    Retries are the caller's concern (connectors keep the retry loop);
    this layer guarantees the security and rate-limit invariants.
    """

    registry: RegistryInfo
    min_interval_seconds: float = 0.0
    timeout_seconds: float | None = None
    _next_allowed_at: float = field(default=0.0, init=False, repr=False)
    _throttle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def headers_for(self, url: str) -> dict[str, str]:
        """Auth headers only when the URL points at the registry's own host."""
        if urlparse(url).netloc in self.registry.hosts:
            return build_auth_headers(self.registry.auth_config)
        return {}

    async def _throttle(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        async with self._throttle_lock:
            now = monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed_at = max(now, self._next_allowed_at) + self.min_interval_seconds

    async def get(self, url: str, *, accept: str | None = None) -> httpx.Response:
        client = get_shared_client(self.timeout_seconds)
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await self._throttle()
            headers = self.headers_for(current)
            if accept:
                headers["Accept"] = accept
            response = await client.get(current, headers=headers)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return response
                current = urljoin(current, location)
                continue
            if response.status_code in (401, 403):
                raise RegistryAuthError(
                    f"registry {self.registry.slug} rejected credentials ({response.status_code}) for {current}"
                )
            return response
        raise httpx.TooManyRedirects(f"exceeded {MAX_REDIRECTS} redirects for {url}")
