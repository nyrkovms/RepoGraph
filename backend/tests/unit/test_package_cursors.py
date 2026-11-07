"""A forged cursor of /packages and /search answers 422, not 500 (the payload is ours to check, not to trust)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import packages, search
from app.db import get_db_session
from app.services.cursor import encode_cursor

_ID = "7f5b1c1e-52a0-4d0b-9d1e-0a6f0a3f4b11"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(packages.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: None   # a bad cursor is rejected before any query
    return TestClient(app)


CASES = [
    # name
    ("name", {"id": "nope", "key": "a"}),
    ("name", {"id": 5, "key": "a"}),
    ("name", {"key": "a"}),
    ("name", {"id": _ID, "key": 5}),
    ("name", {"id": _ID}),
    ("name", {"id": _ID, "key": ["a"]}),
    # relevance (with a query)
    ("relevance", {"id": "nope", "key": "a", "rank": 0}),
    ("relevance", {"id": _ID, "key": 5, "rank": 0}),
    ("relevance", {"id": _ID, "key": "a", "rank": "x"}),
    ("relevance", {"id": _ID, "key": "a", "rank": True}),
    ("relevance", {"id": _ID, "key": "a"}),
    # used_by
    ("used_by", {"id": "nope", "key": "a", "used_by": 1}),
    ("used_by", {"id": _ID, "key": "a", "used_by": "x"}),
    ("used_by", {"id": _ID, "key": "a", "used_by": None}),
    ("used_by", {"id": _ID, "key": 5, "used_by": 1}),
    ("used_by", {"id": _ID, "key": "a"}),
    # updated_at
    ("updated_at", {"id": "nope", "key": "2026-01-01T00:00:00+00:00"}),
    ("updated_at", {"id": _ID, "key": 5}),
    ("updated_at", {"id": _ID, "key": "garbage"}),
    ("updated_at", {"id": _ID, "key": "2026-01-01T00:00:00"}),   # no timezone: the column has one
    # downloads_total
    ("downloads_total", {"id": "nope", "key": "5"}),
    ("downloads_total", {"id": _ID, "key": "abc"}),
    ("downloads_total", {"id": _ID, "key": 5}),
    ("downloads_total", {"id": _ID, "key": None}),
]


@pytest.mark.parametrize(("sort", "payload"), CASES)
def test_forged_cursor_is_422_on_packages(sort: str, payload: dict) -> None:
    cursor = encode_cursor({"sort": sort, **payload})
    params = {"sort": sort, "cursor": cursor}
    if sort == "relevance":
        params["q"] = "x"   # without a query relevance falls back to name, and the cursor is for another sort
        response = _client().get("/api/v1/search", params=params)
    else:
        response = _client().get("/api/v1/packages", params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(("sort", "payload"), [case for case in CASES if case[0] in ("name", "used_by")])
def test_forged_cursor_is_422_on_search(sort: str, payload: dict) -> None:
    response = _client().get("/api/v1/search", params={"q": "x", "sort": sort, "cursor": encode_cursor({"sort": sort, **payload})})
    assert response.status_code == 422, response.text


def test_a_cursor_of_another_sort_is_422() -> None:
    response = _client().get("/api/v1/packages", params={"sort": "name", "cursor": encode_cursor({"sort": "used_by", "id": _ID})})
    assert response.status_code == 422
