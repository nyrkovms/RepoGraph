"""A dependents cursor with a malformed payload answers 422, and so does an unknown `relation`."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.package_detail import router
from app.db import get_db_session
from app.services.cursor import encode_cursor

_URL = "/api/v1/packages/npm/left-pad/-/dependents"
_ID = "7f5b1c1e-52a0-4d0b-9d1e-0a6f0a3f4b11"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: None  # a bad request is rejected before any query
    return TestClient(app)


@pytest.mark.parametrize(
    "payload",
    [
        {"x": 1},
        {"indirect": False, "name": "a"},
        {"indirect": 0, "name": "a", "id": _ID},
        {"indirect": False, "name": 5, "id": _ID},
        {"indirect": False, "name": "a", "id": "not-a-uuid"},
        {"indirect": False, "name": "a", "id": 5},
    ],
)
def test_malformed_cursor_payload_is_422(payload: dict) -> None:
    response = _client().get(_URL, params={"cursor": encode_cursor(payload)})
    assert response.status_code == 422, response.text


def test_undecodable_cursor_is_422() -> None:
    assert _client().get(_URL, params={"cursor": "!!!"}).status_code == 422


@pytest.mark.parametrize("params", [{"relation": "both"}, {"limit": 0}, {"limit": 201}])
def test_bad_parameters_are_422(params: dict) -> None:
    assert _client().get(_URL, params=params).status_code == 422
