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


def test_read_endpoints_are_open_without_token(client: TestClient) -> None:
    for path in ("/api/v1/packages", "/api/v1/search?q=flask", "/api/v1/stats", "/api/v1/updates/summary"):
        assert client.get(path).status_code == 200, path


def test_search_validation_error_for_empty_query(client: TestClient) -> None:
    response = client.get("/api/v1/search", params={"q": ""})
    assert response.status_code == 422


def test_packages_validation_error_for_invalid_sort(client: TestClient) -> None:
    response = client.get(
        "/api/v1/packages",
        params={"sort": "unknown-sort", "limit": 10},
    )
    assert response.status_code == 422


def test_packages_validation_error_for_invalid_limit(client: TestClient) -> None:
    response = client.get(
        "/api/v1/packages",
        params={"sort": "name", "limit": 0},
    )
    assert response.status_code == 422


def test_packages_cursor_sort_mismatch_returns_422(client: TestClient) -> None:
    first = client.get(
        "/api/v1/packages",
        params={"sort": "name", "limit": 1},
    )
    assert first.status_code == 200
    cursor = first.json().get("cursor")
    assert cursor

    second = client.get(
        "/api/v1/packages",
        params={"sort": "updated_at", "limit": 1, "cursor": cursor},
    )
    assert second.status_code == 422
    assert "cursor sort mismatch" in second.text


def test_admin_sync_status_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/admin/sync/status")
    assert response.status_code == 401


def test_admin_refresh_is_gone(client: TestClient, admin_token: str) -> None:
    # The batch refresh was removed (IMPL R3): the daily update replaces it.
    response = client.post("/api/v1/admin/refresh", headers=_auth_headers(admin_token), json={"limit": 5})
    assert response.status_code in (404, 405)


def test_resolve_validation_422_for_empty_dependencies(client: TestClient) -> None:
    payload = _resolve_request_payload()
    payload["dependencies"] = []
    response = client.post("/api/v1/resolve", json=payload)
    assert response.status_code == 422
