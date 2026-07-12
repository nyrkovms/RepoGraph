import asyncio
import json

import httpx
import pytest

from app.connectors.base import PackageNotFound
from app.connectors.npm import NpmConnector, encode_npm_name, parse_manifest_dependencies
from app.fetcher import transport as transport_module
from app.fetcher.transport import RegistryInfo
from app.normalizers.names import normalize_name, npm_scope

REGISTRY = RegistryInfo(
    slug="npm",
    ecosystem="npm",
    protocol="npm",
    base_url="https://registry.npmjs.example/",
)

PACKUMENT = {
    "name": "@acme/toolkit",
    "description": "Acme toolkit",
    "license": "MIT",
    "homepage": "https://acme.example",
    "repository": {"type": "git", "url": "git+https://github.com/acme/toolkit.git"},
    "keywords": ["cli", "tools"],
    "dist-tags": {"latest": "2.0.0", "next": "3.0.0-beta.1"},
    "time": {
        "created": "2024-01-01T00:00:00.000Z",
        "modified": "2026-06-01T00:00:00.000Z",
        "1.0.0": "2024-01-01T00:00:00.000Z",
        "2.0.0": "2026-05-01T00:00:00.000Z",
        "3.0.0-beta.1": "2026-06-01T00:00:00.000Z",
    },
    "maintainers": [{"name": "alice", "email": "alice@acme.example"}],
    "author": {"name": "Acme Inc"},
    "versions": {
        "1.0.0": {
            "version": "1.0.0",
            "deprecated": "use 2.x",
            "dist": {
                "tarball": "https://registry.npmjs.example/@acme/toolkit/-/toolkit-1.0.0.tgz",
                "shasum": "sha1-old",
                "integrity": "sha512-OLD",
            },
        },
        "2.0.0": {
            "version": "2.0.0",
            "dependencies": {"lodash.merge": "^4.6.2", "left-pad": "^1.3.0"},
            "devDependencies": {"vitest": "^3.0.0"},
            "peerDependencies": {"eslint": ">=8"},
            "optionalDependencies": {"fsevents": "^2.3.0"},
            "dist": {
                "tarball": "https://registry.npmjs.example/@acme/toolkit/-/toolkit-2.0.0.tgz",
                "shasum": "sha1-new",
                "integrity": "sha512-NEW",
                "unpackedSize": 123456,
                "fileCount": 42,
            },
            "bin": {"acme": "bin/acme.js"},
        },
        "3.0.0-beta.1": {
            "version": "3.0.0-beta.1",
            "dist": {
                "tarball": "https://registry.npmjs.example/@acme/toolkit/-/toolkit-3.0.0-beta.1.tgz",
                "integrity": "sha512-BETA",
            },
        },
    },
}


def make_connector(monkeypatch, handler) -> NpmConnector:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(transport_module, "get_shared_client", lambda *_a, **_kw: mock_client)
    return NpmConnector(registry=REGISTRY)


def packument_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/@acme/toolkit":
        return httpx.Response(200, content=json.dumps(PACKUMENT), headers={"content-type": "application/json"})
    if request.url.path == "/@acme/toolkit/1.0.0":
        return httpx.Response(
            200, content=json.dumps(PACKUMENT["versions"]["1.0.0"]), headers={"content-type": "application/json"}
        )
    return httpx.Response(404)


def test_packument_parsed(monkeypatch):
    connector = make_connector(monkeypatch, packument_handler)
    package = asyncio.run(connector.fetch_package_metadata("@acme/toolkit"))

    assert package.ecosystem == "npm"
    assert package.namespace == "acme"
    assert package.normalized_name == "@acme/toolkit"
    assert package.latest_version == "2.0.0"
    assert package.dist_tags == {"latest": "2.0.0", "next": "3.0.0-beta.1"}
    assert package.license_raw == "MIT"
    assert package.completeness == "full"
    # packument versions/readme не тащатся в raw_metadata пакета
    assert "versions" not in package.raw_metadata

    versions = {v.version: v for v in package.versions}
    assert set(versions) == {"1.0.0", "2.0.0", "3.0.0-beta.1"}
    assert versions["1.0.0"].status == "deprecated"
    assert versions["1.0.0"].deprecation_message == "use 2.x"
    assert versions["2.0.0"].status == "active"

    artifact = versions["2.0.0"].artifacts[0]
    assert artifact.artifact_type == "tgz"
    assert artifact.checksum_sha1 == "sha1-new"
    assert artifact.checksum_integrity == "sha512-NEW"
    assert artifact.platform_tags == {"unpacked_size": 123456, "file_count": 42}

    roles = {a.role for a in package.authors}
    assert roles == {"author", "maintainer"}


def test_dependencies_only_for_latest_with_types(monkeypatch):
    connector = make_connector(monkeypatch, packument_handler)
    package = asyncio.run(connector.fetch_package_metadata("@acme/toolkit"))
    latest = next(v for v in package.versions if v.version == "2.0.0")
    by_name = {d.dependency_name: d for d in latest.dependencies}
    assert by_name["lodash.merge"].dependency_type == "runtime"
    assert by_name["lodash.merge"].version_constraint == "^4.6.2"
    assert by_name["vitest"].dependency_type == "dev"
    assert by_name["eslint"].dependency_type == "peer"
    assert by_name["fsevents"].dependency_type == "optional"
    assert latest.dependencies_hydrated is True
    old = next(v for v in package.versions if v.version == "1.0.0")
    assert old.dependencies == [] and old.dependencies_hydrated is False


def test_lazy_version_dependencies(monkeypatch):
    connector = make_connector(monkeypatch, packument_handler)
    deps, requires = asyncio.run(connector.fetch_version_dependencies("@acme/toolkit", "1.0.0"))
    assert deps == [] and requires is None


def test_not_found(monkeypatch):
    connector = make_connector(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(PackageNotFound):
        asyncio.run(connector.fetch_package_metadata("missing-pkg"))


def test_optional_not_duplicated_from_dependencies():
    manifest = {
        "dependencies": {"fsevents": "^2.3.0", "chalk": "^5.0.0"},
        "optionalDependencies": {"fsevents": "^2.3.0"},
    }
    deps = parse_manifest_dependencies(manifest)
    fsevents = [d for d in deps if d.dependency_name == "fsevents"]
    assert len(fsevents) == 1 and fsevents[0].dependency_type == "optional"
    assert any(d.dependency_name == "chalk" and d.dependency_type == "runtime" for d in deps)


def test_encode_npm_name():
    assert encode_npm_name("@types/node") == "@types%2Fnode"
    assert encode_npm_name("lodash") == "lodash"


def test_normalize_name_ecosystem_rules():
    # npm: точки значимы, PEP 503 не применяется
    assert normalize_name("npm", "lodash.merge") == "lodash.merge"
    assert normalize_name("npm", "@Scope/Pkg") == "@scope/pkg"
    assert normalize_name("npm", "lodash.merge") != normalize_name("npm", "lodash-merge")
    # pypi: PEP 503 схлопывает разделители
    assert normalize_name("pypi", "Flask_SQLAlchemy") == "flask-sqlalchemy"


def test_npm_scope():
    assert npm_scope("@types/node") == "types"
    assert npm_scope("lodash") is None
