"""HTTP for repository sources, with the record/replay layer of the API (backend/app/services/http.py), same layout:
`<dir>/<host>/<key>.json` + `.body.gz`, so the recorded answers live in the same fixture tree.

Authorization (§6.4): `basic` reads `user:password` from the secret file `<SECRETS_DIR>/<credentials_ref>`,
`tls_client_cert` uses `<credentials_ref>.crt` and `.key` (and `.ca` when present) there. Secrets never reach the
database or a recording.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_KEY_HEADERS = ("accept",)
_WIRE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")
_STORED_RESPONSE_HEADERS = ("content-type", "etag", "last-modified", "location")
USER_AGENT = os.environ.get("HTTP_USER_AGENT", "PackageHub/0.1 (+https://github.com/nyrkovms/RepoGraph)")


class ReplayMiss(httpx.TransportError):
    """No recorded response for the request."""


class SourceAuthError(Exception):
    """The source refused the credentials (401/403) or they are missing: a source error, never "not found"."""


def recording_key(request: httpx.Request) -> str:
    parts = [request.method.upper(), str(request.url)]
    parts += ["%s:%s" % (name, request.headers.get(name, "")) for name in _KEY_HEADERS]
    if request.content:
        parts.append(hashlib.sha256(request.content).hexdigest())
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:24]


def _entry_paths(root: Path, request: httpx.Request) -> Tuple[Path, Path]:
    host = urlsplit(str(request.url)).hostname or "unknown"
    base = root / host / recording_key(request)
    return base.with_suffix(".json"), base.with_suffix(".body.gz")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _not_modified(request: httpx.Request, stored: Dict[str, str]) -> bool:
    etag = stored.get("etag")
    if etag and request.headers.get("if-none-match") == etag:
        return True
    since, modified = request.headers.get("if-modified-since"), stored.get("last-modified")
    if since and modified:
        try:
            return parsedate_to_datetime(modified) <= parsedate_to_datetime(since)
        except (TypeError, ValueError):
            return False
    return False


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, root: Path, inner: httpx.BaseTransport) -> None:
        self.root, self.inner = root, inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        body = response.read()
        if response.status_code != 304:
            meta_path, body_path = _entry_paths(self.root, request)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta = {
                "request": {"method": request.method, "url": str(request.url),
                            "headers": {n: request.headers[n] for n in _KEY_HEADERS if n in request.headers}},
                "status": response.status_code,
                "headers": {n: response.headers[n] for n in _STORED_RESPONSE_HEADERS if n in response.headers},
            }
            _atomic_write(body_path, gzip.compress(body))
            _atomic_write(meta_path, json.dumps(meta, indent=2, sort_keys=True).encode())
        return httpx.Response(response.status_code, request=request, content=body,
                              headers=[(k, v) for k, v in response.headers.multi_items()
                                       if k.lower() not in _WIRE_HEADERS])

    def close(self) -> None:
        self.inner.close()


class ReplayTransport(httpx.BaseTransport):
    def __init__(self, root: Path) -> None:
        self.root = root

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        meta_path, body_path = _entry_paths(self.root, request)
        if not meta_path.exists():
            logger.error("REPLAY_MISS %s %s", request.method, request.url)
            raise ReplayMiss("no recorded response for %s %s" % (request.method, request.url), request=request)
        meta = json.loads(meta_path.read_text())
        headers = meta["headers"]
        if _not_modified(request, headers):
            return httpx.Response(304, request=request,
                                  headers={k: v for k, v in headers.items() if k in ("etag", "last-modified")})
        body = gzip.decompress(body_path.read_bytes()) if body_path.exists() else b""
        return httpx.Response(meta["status"], headers=headers, content=body, request=request)


def _secrets_dir() -> Path:
    return Path(os.environ.get("SECRETS_DIR", "/run/secrets/packagehub"))


def client_options(auth_type: str, credentials_ref: Optional[str]) -> Dict[str, Any]:
    """httpx options for the source's authorization (SPEC §6.4)."""
    if auth_type in (None, "", "none"):
        return {}
    if not credentials_ref:
        raise SourceAuthError("auth_type %s needs a credentials_ref" % auth_type)
    base = _secrets_dir() / credentials_ref
    if auth_type == "basic":
        try:
            user, _, password = base.read_text().strip().partition(":")
        except OSError as exc:
            raise SourceAuthError("secret %s is not readable: %s" % (credentials_ref, exc)) from None
        return {"auth": (user, password)}
    if auth_type == "tls_client_cert":
        cert, key, ca = Path(str(base) + ".crt"), Path(str(base) + ".key"), Path(str(base) + ".ca")
        if not (cert.exists() and key.exists()):
            raise SourceAuthError("secret %s needs %s and %s" % (credentials_ref, cert.name, key.name))
        options: Dict[str, Any] = {"cert": (str(cert), str(key))}
        if ca.exists():
            options["verify"] = str(ca)
        return options
    raise SourceAuthError("auth_type %s is not supported for RPM sources" % auth_type)


def make_client(timeout: float = 60.0, auth_type: str = "none", credentials_ref: Optional[str] = None) -> httpx.Client:
    options = client_options(auth_type, credentials_ref)
    replay, record = os.environ.get("HTTP_REPLAY_DIR"), os.environ.get("HTTP_RECORD_DIR")
    transport: Optional[httpx.BaseTransport] = None
    if replay:
        transport = ReplayTransport(Path(replay))
    elif record:
        inner = httpx.HTTPTransport(cert=options.get("cert"), verify=options.get("verify", True))
        transport = RecordingTransport(Path(record), inner)
    elif "cert" in options or "verify" in options:
        transport = httpx.HTTPTransport(cert=options.get("cert"), verify=options.get("verify", True))
    return httpx.Client(timeout=timeout, transport=transport, auth=options.get("auth"), follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def get(client: httpx.Client, url: str, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    """404/410 and 304 are answers; 401/403 are authorization failures of the source; other errors raise."""
    response = client.get(url, headers=headers or {})
    if response.status_code in (401, 403):
        raise SourceAuthError("%s answered %s" % (url, response.status_code))
    if response.status_code not in (304, 404, 410):
        response.raise_for_status()
    return response
