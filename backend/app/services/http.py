"""HTTP clients for upstream sources, with record/replay of responses for offline tests (IMPL P0.5/P0.6).

HTTP_RECORD_DIR: every upstream response is also saved under the directory.
HTTP_REPLAY_DIR: responses come only from the directory; a request that was never recorded fails with ReplayMiss
instead of reaching the network, and is logged as REPLAY_MISS so CI can fail on it even when callers swallow
the exception (seed records it as a sync failure, lazy fetch returns "error").
"""

import gzip
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Headers that change the representation and therefore take part in the recording key.
_KEY_HEADERS = ("accept",)
# The body handed back after aread() is already decoded; these headers describe the wire form and must go.
_WIRE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")
_STORED_RESPONSE_HEADERS = ("content-type", "etag", "last-modified", "x-pypi-last-serial", "location")


class ReplayMiss(httpx.TransportError):
    """No recorded response for the request. Not in the retryable set, so nothing retries it."""


def recording_key(request: httpx.Request) -> str:
    parts = [request.method.upper(), str(request.url)]
    parts += [f"{name}:{request.headers.get(name, '')}" for name in _KEY_HEADERS]
    if request.content:
        # POST bodies (e.g. batch queries) distinguish otherwise identical requests.
        parts.append(hashlib.sha256(request.content).hexdigest())
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:24]


def _entry_paths(root: Path, request: httpx.Request) -> tuple[Path, Path]:
    host = urlsplit(str(request.url)).hostname or "unknown"
    base = root / host / recording_key(request)
    return base.with_suffix(".json"), base.with_suffix(".body.gz")


def _atomic_write(path: Path, data: bytes) -> None:
    # Several processes record into one directory; readers must never see a half-written file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _not_modified(request: httpx.Request, stored_headers: dict[str, str]) -> bool:
    etag = stored_headers.get("etag")
    if etag and request.headers.get("if-none-match") == etag:
        return True
    since, modified = request.headers.get("if-modified-since"), stored_headers.get("last-modified")
    if since and modified:
        try:
            return parsedate_to_datetime(modified) <= parsedate_to_datetime(since)
        except (TypeError, ValueError):
            return False
    return False


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
            meta = {
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": {name: request.headers[name] for name in _KEY_HEADERS if name in request.headers},
                },
                "status": response.status_code,
                "headers": {name: response.headers[name] for name in _STORED_RESPONSE_HEADERS if name in response.headers},
            }
            # Body first: a meta file is the marker that a complete entry exists.
            _atomic_write(body_path, gzip.compress(body))
            _atomic_write(meta_path, json.dumps(meta, indent=2, sort_keys=True).encode())
        return httpx.Response(
            status_code=response.status_code,
            headers=[(k, v) for k, v in response.headers.multi_items() if k.lower() not in _WIRE_HEADERS],
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
            logger.error("REPLAY_MISS %s %s", request.method, request.url)
            raise ReplayMiss(f"no recorded response for {request.method} {request.url}", request=request)
        meta = json.loads(meta_path.read_text())
        headers = meta["headers"]
        if _not_modified(request, headers):
            return httpx.Response(
                304, headers={k: v for k, v in headers.items() if k in ("etag", "last-modified")}, request=request
            )
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
    extra = kwargs.pop("headers", None)
    headers = {"User-Agent": settings.http_user_agent, **(dict(extra) if isinstance(extra, Mapping) else {})}
    return httpx.AsyncClient(timeout=timeout, transport=transport, headers=headers, **kwargs)  # type: ignore[arg-type]
