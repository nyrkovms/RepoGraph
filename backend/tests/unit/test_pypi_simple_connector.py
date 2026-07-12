import asyncio
import json

import httpx
import pytest

from app.connectors.base import PackageNotFound
from app.connectors.pypi_simple import PyPISimpleConnector, _parse_filename_version, _pick_latest
from app.fetcher import transport as transport_module
from app.fetcher.transport import RegistryInfo

REGISTRY = RegistryInfo(
    slug="pytorch",
    ecosystem="pypi",
    protocol="pypi-simple",
    base_url="https://download.pytorch.example/whl/",
)

PEP691_PROJECT = {
    "meta": {"api-version": "1.0"},
    "name": "torchlike",
    "files": [
        {
            "filename": "torchlike-2.1.0-py3-none-any.whl",
            "url": "https://download.pytorch.example/whl/torchlike-2.1.0-py3-none-any.whl",
            "hashes": {"sha256": "aaa111"},
            "requires-python": ">=3.8",
            "yanked": False,
            "size": 1000,
            "upload-time": "2026-01-10T00:00:00Z",
            "core-metadata": {"sha256": "cafe"},
        },
        {
            "filename": "torchlike-2.1.0+cu121-cp312-cp312-linux_x86_64.whl",
            "url": "https://download.pytorch.example/whl/cu121/torchlike-2.1.0%2Bcu121-cp312-cp312-linux_x86_64.whl",
            "hashes": {"sha256": "bbb222"},
            "requires-python": ">=3.8",
            "yanked": False,
            "core-metadata": {"sha256": "beef"},
        },
        {
            "filename": "torchlike-1.9.0-py3-none-any.whl",
            "url": "https://download.pytorch.example/whl/torchlike-1.9.0-py3-none-any.whl",
            "hashes": {},
            "yanked": True,
        },
        {
            "filename": "not-a-dist.txt",
            "url": "https://download.pytorch.example/whl/not-a-dist.txt",
            "hashes": {},
        },
    ],
}

CORE_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: torchlike\n"
    "Version: 2.1.0\n"
    "Requires-Python: >=3.8\n"
    "Requires-Dist: filelock\n"
    "Requires-Dist: sympy>=1.12; python_version >= \"3.9\"\n"
    "\n"
    "Long description here.\n"
)

PEP503_PROJECT_HTML = """
<!DOCTYPE html><html><body>
<a href="../pkg/legacy_pkg-0.5.0-py3-none-any.whl#sha256=ccc333"
   data-requires-python="&gt;=3.7">legacy_pkg-0.5.0-py3-none-any.whl</a><br/>
<a href="../pkg/legacy_pkg-0.4.0.tar.gz#sha256=ddd444" data-yanked="reason">legacy_pkg-0.4.0.tar.gz</a><br/>
</body></html>
"""

ROOT_JSON = {"meta": {"api-version": "1.0"}, "projects": [{"name": "TorchLike"}, {"name": "legacy_pkg"}]}
ROOT_HTML = '<html><body><a href="/whl/torchlike/">torchlike</a><a href="/whl/legacy-pkg/">legacy-pkg</a></body></html>'


def make_connector(monkeypatch, handler) -> PyPISimpleConnector:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(transport_module, "get_shared_client", lambda *_a, **_kw: mock_client)
    return PyPISimpleConnector(registry=REGISTRY)


def json_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/whl/torchlike/":
        return httpx.Response(200, content=json.dumps(PEP691_PROJECT), headers={"content-type": "application/vnd.pypi.simple.v1+json"})
    if path.endswith(".whl.metadata"):
        return httpx.Response(200, text=CORE_METADATA)
    if path == "/whl/":
        return httpx.Response(200, content=json.dumps(ROOT_JSON), headers={"content-type": "application/vnd.pypi.simple.v1+json"})
    return httpx.Response(404)


def test_json_project_page_parsed(monkeypatch):
    connector = make_connector(monkeypatch, json_handler)
    package = asyncio.run(connector.fetch_package_metadata("torchlike"))

    assert package.completeness == "minimal"
    assert package.normalized_name == "torchlike"
    versions = {v.version: v for v in package.versions}
    # локальная версия +cu121 — отдельная версия (кейс PyTorch index)
    assert set(versions) == {"2.1.0", "2.1.0+cu121", "1.9.0"}
    assert package.latest_version == "2.1.0+cu121"
    assert versions["1.9.0"].status == "yanked"
    assert versions["2.1.0"].requires_python == ">=3.8"
    any_wheel = versions["2.1.0"].artifacts[0]
    assert any_wheel.checksum_sha256 == "aaa111"
    assert any_wheel.artifact_type == "wheel"
    # мусорный файл без версии отброшен
    assert all("not-a-dist" not in a.filename for v in package.versions for a in v.artifacts)


def test_pep658_dependencies_for_latest(monkeypatch):
    connector = make_connector(monkeypatch, json_handler)
    package = asyncio.run(connector.fetch_package_metadata("torchlike"))
    latest = next(v for v in package.versions if v.version == package.latest_version)
    dep_names = {d.dependency_name for d in latest.dependencies}
    assert dep_names == {"filelock", "sympy"}
    assert latest.dependencies_hydrated is True
    # зависимости прикрепляются только к latest; остальные версии — lazy
    other = next(v for v in package.versions if v.version == "2.1.0")
    assert other.dependencies == []
    assert other.dependencies_hydrated is False


def test_html_project_page_parsed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/whl/legacy-pkg/":
            return httpx.Response(200, text=PEP503_PROJECT_HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    connector = make_connector(monkeypatch, handler)
    package = asyncio.run(connector.fetch_package_metadata("legacy-pkg"))

    versions = {v.version: v for v in package.versions}
    assert set(versions) == {"0.5.0", "0.4.0"}
    wheel = versions["0.5.0"].artifacts[0]
    assert wheel.checksum_sha256 == "ccc333"
    assert wheel.download_url == "https://download.pytorch.example/whl/pkg/legacy_pkg-0.5.0-py3-none-any.whl"
    assert versions["0.5.0"].requires_python == ">=3.7"
    assert versions["0.4.0"].status == "yanked"
    assert package.latest_version == "0.5.0"


def test_package_not_found(monkeypatch):
    connector = make_connector(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(PackageNotFound):
        asyncio.run(connector.fetch_package_metadata("missing"))


def test_list_package_names_json(monkeypatch):
    connector = make_connector(monkeypatch, json_handler)
    names = asyncio.run(connector.list_package_names())
    assert names == ["legacy-pkg", "torchlike"]  # canonicalized + sorted


def test_list_package_names_html(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=ROOT_HTML, headers={"content-type": "text/html"})

    connector = make_connector(monkeypatch, handler)
    names = asyncio.run(connector.list_package_names())
    assert names == ["legacy-pkg", "torchlike"]


def test_filename_version_parsing():
    assert _parse_filename_version("pkg-1.2.3-py3-none-any.whl") == "1.2.3"
    assert _parse_filename_version("pkg-1.2.3.tar.gz") == "1.2.3"
    assert _parse_filename_version("torchlike-2.1.0+cu121-cp312-cp312-linux_x86_64.whl") == "2.1.0+cu121"
    assert _parse_filename_version("garbage.txt") is None


def test_pick_latest_prefers_stable():
    assert _pick_latest(["1.0.0", "2.0.0rc1"]) == "1.0.0"
    assert _pick_latest(["9.0", "10.0"]) == "10.0"
