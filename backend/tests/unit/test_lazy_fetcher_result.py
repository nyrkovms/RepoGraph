import asyncio

import httpx

from app.connectors.base import BaseConnector, PackageNotFound
from app.fetcher.lazy import LazyFetcher
from app.fetcher.transport import RegistryAuthError


class StubDB:
    """find_package(db, ...) → db.scalar(select) → None: пакета в БД нет."""

    async def scalar(self, _query):
        return None


class FailingConnector(BaseConnector):
    ecosystem = "pypi"

    def __init__(self, exc: Exception):
        self.exc = exc

    async def fetch_package_metadata(self, name: str):
        raise self.exc

    def get_rate_limit(self) -> float:
        return 0.0


def run(connector_exc: Exception):
    fetcher = LazyFetcher(connector=FailingConnector(connector_exc), db=StubDB())
    return asyncio.run(fetcher.ensure_package_result("some-package"))


def test_not_found_is_typed():
    result = run(PackageNotFound("some-package"))
    assert result.status == "not_found"
    assert result.package is None


def test_auth_error_is_not_not_found():
    result = run(RegistryAuthError("401 from registry"))
    assert result.status == "auth_error"
    assert "401" in result.error


def test_upstream_error_is_typed():
    result = run(httpx.ConnectTimeout("boom"))
    assert result.status == "upstream_error"


def test_backward_compatible_wrapper_returns_none_on_error():
    fetcher = LazyFetcher(connector=FailingConnector(PackageNotFound("x")), db=StubDB())
    assert asyncio.run(fetcher.ensure_package("x")) is None
