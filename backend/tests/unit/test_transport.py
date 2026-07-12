import asyncio
from time import monotonic

import httpx
import pytest

from app.fetcher import transport as transport_module
from app.fetcher.transport import (
    RegistryAuthError,
    RegistryClient,
    RegistryInfo,
    build_auth_headers,
)

REGISTRY = RegistryInfo(
    slug="internal",
    ecosystem="pypi",
    protocol="pypi-simple",
    base_url="https://nexus.corp/repository/pypi-hosted/simple/",
    auth_config={"type": "bearer", "token": "s3cret"},
)


def make_client(monkeypatch, handler) -> RegistryClient:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(transport_module, "get_shared_client", lambda *_args, **_kw: mock_client)
    return RegistryClient(registry=REGISTRY)


def test_build_auth_headers_bearer():
    assert build_auth_headers({"type": "bearer", "token": "abc"}) == {"Authorization": "Bearer abc"}


def test_build_auth_headers_basic():
    headers = build_auth_headers({"type": "basic", "username": "u", "password": "p"})
    assert headers == {"Authorization": "Basic dTpw"}


def test_build_auth_headers_empty_and_unknown():
    assert build_auth_headers(None) == {}
    assert build_auth_headers({}) == {}
    assert build_auth_headers({"type": "kerberos"}) == {}


def test_headers_for_registry_host_vs_cdn():
    client = RegistryClient(registry=REGISTRY)
    assert "Authorization" in client.headers_for("https://nexus.corp/repository/pypi-hosted/simple/six/")
    assert client.headers_for("https://cdn.example.net/packages/six.whl") == {}


def test_auth_sent_only_to_registry_host(monkeypatch):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        if request.url.host == "nexus.corp":
            return httpx.Response(302, headers={"location": "https://cdn.example.net/blob.whl"})
        return httpx.Response(200, content=b"wheel-bytes")

    client = make_client(monkeypatch, handler)
    response = asyncio.run(client.get("https://nexus.corp/repository/pypi-hosted/simple/blob.whl"))

    assert response.status_code == 200
    assert seen["nexus.corp"] == "Bearer s3cret"
    assert seen["cdn.example.net"] is None  # токен не ушёл на CDN


def test_relative_redirect_followed_with_auth(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/old/"):
            return httpx.Response(301, headers={"location": "../new/"})
        assert request.headers.get("authorization") == "Bearer s3cret"
        return httpx.Response(200, text="ok")

    client = make_client(monkeypatch, handler)
    response = asyncio.run(client.get("https://nexus.corp/repository/pypi-hosted/simple/old/"))
    assert response.status_code == 200
    assert calls[-1].endswith("/simple/new/")


def test_auth_error_raised(monkeypatch):
    client = make_client(monkeypatch, lambda request: httpx.Response(401))
    with pytest.raises(RegistryAuthError):
        asyncio.run(client.get("https://nexus.corp/repository/pypi-hosted/simple/six/"))


def test_redirect_loop_capped(monkeypatch):
    client = make_client(
        monkeypatch, lambda request: httpx.Response(302, headers={"location": str(request.url)})
    )
    with pytest.raises(httpx.TooManyRedirects):
        asyncio.run(client.get("https://nexus.corp/loop"))


def test_throttle_enforces_min_interval(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport_module, "get_shared_client", lambda *_a, **_kw: mock_client)
    client = RegistryClient(registry=REGISTRY, min_interval_seconds=0.05)

    async def three_requests() -> float:
        start = monotonic()
        for _ in range(3):
            await client.get("https://nexus.corp/x")
        return monotonic() - start

    elapsed = asyncio.run(three_requests())
    assert elapsed >= 0.10  # 3 запроса, интервал 50мс → минимум 100мс


def test_shared_client_reused_within_loop():
    async def scenario():
        first = transport_module.get_shared_client()
        second = transport_module.get_shared_client()
        assert first is second
        await transport_module.aclose_shared_client()
        third = transport_module.get_shared_client()
        assert third is not first
        await transport_module.aclose_shared_client()

    asyncio.run(scenario())
