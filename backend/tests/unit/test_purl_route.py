"""The purl redirect reads qualifiers from the query string too (#41)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.package_detail import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_qualifiers_written_as_a_query_string_reach_the_purl() -> None:
    client = _client()
    for path in ("/api/v1/purl/pkg:rpm/rocky/bash?distro=rocky-9", "/api/v1/purl/pkg:rpm/rocky/bash%3Fdistro%3Drocky-9"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307, response.text
        assert response.headers["location"] == "/api/v1/packages/rpm/rocky-9/bash/-/"
    version = client.get("/api/v1/purl/pkg:rpm/rocky/openssl@1:3.0.7-27.el9?arch=x86_64&distro=rocky-9",
                         follow_redirects=False)
    assert version.headers["location"] == "/api/v1/packages/rpm/rocky-9/openssl/-/versions/1%3A3.0.7-27.el9"
    assert client.get("/api/v1/purl/pkg:rpm/rocky/bash", follow_redirects=False).status_code == 422
