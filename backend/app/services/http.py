"""HTTP clients for upstream sources, with record/replay of responses for offline tests (IMPL P0.5/P0.6).

HTTP_RECORD_DIR: every upstream response is also saved under the directory.
HTTP_REPLAY_DIR: responses come only from the directory; a request that was never recorded fails with ReplayMiss
instead of reaching the network.
"""

import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import get_settings

# Headers that change the representation and therefore take part in the recording key.
_KEY_HEADERS = ("accept",)
_CONDITIONAL_HEADERS = ("if-none-match", "if-modified-since")
_STORED_RESPONSE_HEADERS = ("content-type", "etag", "last-modified", "x-pypi-last-serial", "location")


class ReplayMiss(httpx.TransportError):
    """No recorded response for the request. Not in the retryable set: tests fail at once with the URL."""


def recording_key(request: httpx.Request) -> str:
    parts = [request.method.upper(), str(request.url)]
    parts += [f"{name}:{request.headers.get(name, '')}" for name in _KEY_HEADERS]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:24]


def _entry_paths(root: Path, request: httpx.Request) -> tuple[Path, Path]:
    host = urlsplit(str(request.url)).hostname or "unknown"
    base = root / host / recording_key(request)
    return base.with_suffix(".json"), base.with_suffix(".body.gz")


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, root: Path, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.root = root
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        # A 304 says nothing about the content; keep the full response recorded earlier.
        if response.status_code != 304:
            meta_path, body_path = _entry_paths(self.root, request)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(gzip.compress(body))
            meta = {
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": {name: request.headers[name] for name in _KEY_HEADERS if name in request.headers},
                },
                "status": response.status_code,
                "headers": {name: response.headers[name] for name in _STORED_RESPONSE_HEADERS if name in response.headers},
            }
            meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, root: Path) -> None:
        self.root = root

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        meta_path, body_path = _entry_paths(self.root, request)
        if not meta_path.exists():
            raise ReplayMiss(f"no recorded response for {request.method} {request.url}", request=request)
        meta = json.loads(meta_path.read_text())
        headers = meta["headers"]
        etag = headers.get("etag")
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag}, request=request)
        body = gzip.decompress(body_path.read_bytes()) if body_path.exists() else b""
        return httpx.Response(meta["status"], headers=headers, content=body, request=request)


def make_client(timeout: float, **kwargs: object) -> httpx.AsyncClient:
    """The only way application code should create a client for upstream sources."""
    settings = get_settings()
    transport: httpx.AsyncBaseTransport | None = None
    if settings.http_replay_dir:
        transport = ReplayTransport(Path(settings.http_replay_dir))
    elif settings.http_record_dir:
        transport = RecordingTransport(Path(settings.http_record_dir))
    return httpx.AsyncClient(timeout=timeout, transport=transport, **kwargs)  # type: ignore[arg-type]
