import pytest
from fastapi.testclient import TestClient

from app.api.v1 import resolve as resolve_api
from app.resolver.errors import (
    ResolveConflictError,
    ResolveInternalError,
    ResolveNotFoundError,
    ResolveTimeoutError,
)


def _auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _resolve_request_payload() -> dict:
    return {
        "ecosystem": "pypi",
        "dependencies": [{"name": "flask", "version_spec": ">=3.0"}],
        "environment": {
            "python_version": "3.12",
            "sys_platform": "linux",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_system": "Linux",
            "implementation_name": "cpython",
        },
        "options": {"include_dev": False, "max_depth": 30, "max_rounds": 200},
    }


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("Referrer-Policy") == "no-referrer"


def test_stats_returns_counts(client: TestClient) -> None:
    response = client.get("/api/v1/stats")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ecosystem"] == "pypi"
    assert payload["packages"] >= 1
    assert payload["versions"] >= 1


def test_catalog_and_package_endpoints(client: TestClient) -> None:
    packages = client.get("/api/v1/packages", params={"limit": 5, "sort": "name"})
    search = client.get("/api/v1/search", params={"q": "flask", "limit": 5})
    package = client.get("/api/v1/packages/pypi/flask")

    assert packages.status_code == 200
    list_payload = packages.json()
    assert isinstance(list_payload["data"], list)
    assert len(list_payload["data"]) >= 1

    assert search.status_code == 200
    search_payload = search.json()
    assert any("flask" in row["normalized_name"] for row in search_payload["data"])

    assert package.status_code == 200
    package_payload = package.json()
    assert package_payload["normalized_name"] == "flask"
    assert package_payload["latest_version"]


def test_resolve_endpoint_returns_result(client: TestClient) -> None:
    response = client.post("/api/v1/resolve", json=_resolve_request_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["total_packages"] >= 1
    assert "flask" in payload["resolved"]


def test_resolve_not_found_error_mapping(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_resolve(_db, _payload):
        raise ResolveNotFoundError("missing-pkg")

    monkeypatch.setattr(resolve_api, "resolve_dependencies", _fake_resolve)
    response = client.post("/api/v1/resolve", json=_resolve_request_payload())
    payload = response.json()

    assert response.status_code == 404
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["details"]["package"] == "missing-pkg"


def test_resolve_conflict_error_mapping(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_resolve(_db, _payload):
        raise ResolveConflictError("dependency conflict", details={"root": "flask"})

    monkeypatch.setattr(resolve_api, "resolve_dependencies", _fake_resolve)
    response = client.post("/api/v1/resolve", json=_resolve_request_payload())
    payload = response.json()

    assert response.status_code == 409
    assert payload["error"]["code"] == "resolver_conflict"
    assert payload["error"]["details"] == {"root": "flask"}


def test_resolve_timeout_error_mapping(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_resolve(_db, _payload):
        raise ResolveTimeoutError("resolve took longer than 30 s")

    monkeypatch.setattr(resolve_api, "resolve_dependencies", _fake_resolve)
    response = client.post("/api/v1/resolve", json=_resolve_request_payload())
    payload = response.json()

    assert response.status_code == 504
    assert payload["error"]["code"] == "resolve_timeout"


def test_resolve_internal_error_mapping(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_resolve(_db, _payload):
        raise ResolveInternalError("internal blowup")

    monkeypatch.setattr(resolve_api, "resolve_dependencies", _fake_resolve)
    response = client.post("/api/v1/resolve", json=_resolve_request_payload())
    payload = response.json()

    assert response.status_code == 500
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"] == "internal blowup"


def test_admin_sync_status_requires_admin_token(client: TestClient, admin_token: str) -> None:
    anonymous_resp = client.get("/api/v1/admin/sync/status")
    wrong_resp = client.get("/api/v1/admin/sync/status", headers=_auth_headers("not-an-admin-token"))
    admin_resp = client.get("/api/v1/admin/sync/status", headers=_auth_headers(admin_token))

    assert anonymous_resp.status_code == 401
    assert wrong_resp.status_code == 401
    assert admin_resp.status_code == 200
    payload = admin_resp.json()
    assert "status" in payload
    assert "sync_type" in payload


def test_metrics_endpoint_contains_core_metrics(client: TestClient) -> None:
    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.text
    assert "packagehub_api_latency_seconds" in body
    assert "packagehub_resolve_latency_seconds" in body
    assert "packagehub_sync_duration_seconds" in body
