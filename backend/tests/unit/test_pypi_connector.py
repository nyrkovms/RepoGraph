import asyncio
from unittest.mock import AsyncMock

from app.config import get_settings
from app.connectors.pypi import PyPIConnector


def test_normalize_vulnerabilities_falls_back_from_null_summary() -> None:
    connector = PyPIConnector()
    result = connector._normalize_vulnerabilities(
        [
            {
                "id": "GHSA-test-1",
                "summary": None,
                "details": "Detailed advisory text",
                "aliases": None,
            }
        ]
    )

    assert len(result) == 1
    assert result[0].source_id == "GHSA-test-1"
    assert result[0].summary == "Detailed advisory text"
    assert result[0].aliases == []


def test_normalize_vulnerabilities_uses_default_when_summary_and_details_absent() -> None:
    connector = PyPIConnector()
    result = connector._normalize_vulnerabilities([{"id": "GHSA-test-2", "summary": None, "details": None}])

    assert len(result) == 1
    assert result[0].summary == "Advisory GHSA-test-2"


def test_normalize_versions_marks_only_latest_as_hydrated() -> None:
    connector = PyPIConnector()
    latest_dependencies = connector._normalize_versions(
        releases={
            "2.0.0": [{"filename": "pkg-2.0.0.tar.gz", "packagetype": "sdist", "upload_time_iso_8601": "2026-01-02T00:00:00Z"}],
            "1.0.0": [{"filename": "pkg-1.0.0.tar.gz", "packagetype": "sdist", "upload_time_iso_8601": "2025-01-02T00:00:00Z"}],
        },
        latest_version="2.0.0",
        latest_dependencies=[],
    )

    by_version = {row.version: row for row in latest_dependencies}
    assert by_version["2.0.0"].dependencies_hydrated is True
    assert by_version["1.0.0"].dependencies_hydrated is False


def test_fetch_version_dependencies_uses_version_specific_pypi_payload(monkeypatch) -> None:
    connector = PyPIConnector()

    class _Response:
        def json(self) -> dict:
            return {
                "info": {
                    "requires_python": ">=3.10",
                    "requires_dist": [
                        "asgiref>=3.8.1",
                        "argon2-cffi>=19.1.0; extra == 'argon2'",
                    ],
                }
            }

    monkeypatch.setattr(connector, "_fetch_with_retry", AsyncMock(return_value=_Response()))

    dependencies, requires_python = asyncio.run(connector.fetch_version_dependencies("django", "5.2.7"))

    assert requires_python == ">=3.10"
    assert [dep.dependency_name for dep in dependencies] == ["asgiref", "argon2-cffi"]
    assert dependencies[0].version_constraint == ">=3.8.1"
    assert dependencies[1].environment_markers["expression"] == "extra == \"argon2\""


def test_connector_uses_sync_pypi_json_base_url_from_settings(monkeypatch) -> None:
    monkeypatch.setenv("SYNC_PYPI_JSON_BASE_URL", "https://proxy.internal/pypi/")
    get_settings.cache_clear()
    connector = PyPIConnector()

    class _Response:
        def json(self) -> dict:
            return {
                "info": {
                    "name": "Flask",
                    "version": "3.1.3",
                    "summary": "A web framework",
                    "classifiers": [],
                    "project_urls": {},
                },
                "releases": {
                    "3.1.3": [
                        {
                            "filename": "Flask-3.1.3.tar.gz",
                            "packagetype": "sdist",
                            "upload_time_iso_8601": "2026-01-01T00:00:00Z",
                            "requires_python": ">=3.9",
                            "digests": {},
                            "yanked": False,
                        }
                    ]
                },
                "vulnerabilities": [],
            }

    monkeypatch.setattr(connector, "_fetch_with_retry", AsyncMock(return_value=_Response()))

    asyncio.run(connector.fetch_package_metadata("flask"))

    connector._fetch_with_retry.assert_awaited_once_with("https://proxy.internal/pypi/flask/json", "flask")
    get_settings.cache_clear()
