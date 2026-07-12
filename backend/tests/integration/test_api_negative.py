from fastapi.testclient import TestClient


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


def test_packages_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/packages")
    assert response.status_code == 401


def test_search_validation_error_for_empty_query(client: TestClient, viewer_token: str) -> None:
    response = client.get("/api/v1/search", headers=_auth_headers(viewer_token), params={"q": ""})
    assert response.status_code == 422


def test_packages_validation_error_for_invalid_sort(client: TestClient, viewer_token: str) -> None:
    response = client.get(
        "/api/v1/packages",
        headers=_auth_headers(viewer_token),
        params={"sort": "unknown-sort", "limit": 10},
    )
    assert response.status_code == 422


def test_packages_validation_error_for_invalid_limit(client: TestClient, viewer_token: str) -> None:
    response = client.get(
        "/api/v1/packages",
        headers=_auth_headers(viewer_token),
        params={"sort": "name", "limit": 0},
    )
    assert response.status_code == 422


def test_packages_cursor_sort_mismatch_returns_422(client: TestClient, viewer_token: str) -> None:
    first = client.get(
        "/api/v1/packages",
        headers=_auth_headers(viewer_token),
        params={"sort": "name", "limit": 1},
    )
    assert first.status_code == 200
    cursor = first.json().get("cursor")
    assert cursor

    second = client.get(
        "/api/v1/packages",
        headers=_auth_headers(viewer_token),
        params={"sort": "updated_at", "limit": 1, "cursor": cursor},
    )
    assert second.status_code == 422
    assert "cursor sort mismatch" in second.text


def test_admin_sync_status_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/admin/sync/status")
    assert response.status_code == 401


def test_admin_refresh_forbidden_for_viewer(client: TestClient, viewer_token: str) -> None:
    response = client.post("/api/v1/admin/refresh", headers=_auth_headers(viewer_token), json={"limit": 5})
    assert response.status_code == 403


def test_admin_refresh_validation_422(client: TestClient, admin_token: str) -> None:
    response = client.post("/api/v1/admin/refresh", headers=_auth_headers(admin_token), json={"limit": 0})
    assert response.status_code == 422


def test_resolve_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/resolve", json=_resolve_request_payload())
    assert response.status_code == 401


def test_resolve_validation_422_for_empty_dependencies(client: TestClient, viewer_token: str) -> None:
    payload = _resolve_request_payload()
    payload["dependencies"] = []
    response = client.post("/api/v1/resolve", headers=_auth_headers(viewer_token), json=payload)
    assert response.status_code == 422
