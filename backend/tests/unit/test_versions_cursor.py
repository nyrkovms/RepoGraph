"""A versions cursor with a malformed payload answers 422, like /packages and /updates (#53)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.package_detail import router
from app.db import get_db_session
from app.services.cursor import encode_cursor

_URL = "/api/v1/packages/pypi/flask/-/versions"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: None  # a bad cursor is rejected before any query
    return TestClient(app)


@pytest.mark.parametrize(
    "payload",
    [
        {"x": 1},
        {"release_date": None},
        {"id": "not-a-uuid", "release_date": None},
        {"id": 5, "release_date": None},
        {"id": "7f5b1c1e-52a0-4d0b-9d1e-0a6f0a3f4b11", "release_date": 12345},
        {"id": "7f5b1c1e-52a0-4d0b-9d1e-0a6f0a3f4b11", "release_date": [1]},
        {"id": "7f5b1c1e-52a0-4d0b-9d1e-0a6f0a3f4b11", "release_date": "garbage"},
    ],
)
def test_malformed_cursor_payload_is_422(payload: dict) -> None:
    response = _client().get(_URL, params={"cursor": encode_cursor(payload)})
    assert response.status_code == 422, response.text


def test_undecodable_cursor_is_422() -> None:
    assert _client().get(_URL, params={"cursor": "!!!"}).status_code == 422
