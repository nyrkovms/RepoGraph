import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def viewer_token() -> str:
    return os.getenv("API_TOKEN_VIEWER", "phub_viewer_dev")


@pytest.fixture(scope="session")
def admin_token() -> str:
    return os.getenv("API_TOKEN_ADMIN", "phub_admin_dev")

