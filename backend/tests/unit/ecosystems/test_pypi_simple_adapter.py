"""The `pypi-simple` adapter (WP D1) on trimmed copies of real index pages (tests/fixtures/pypi_simple/):
download.pytorch.org (`/whl/cu121/torch/` without PEP 658 metadata, `/whl/cpu/torch/` with it, other host, `%2B`),
the cpu index copy of filelock (data-requires-python, data-yanked), pypi.org's PEP 691 JSON for mpmath.
The `.metadata` fixtures are the real headers of torch 2.9.1+cpu (Linux and Windows wheels) without the long
description; the page hashes of those files were recomputed for the trimmed content."""

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.ecosystems.base import PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.pypi.adapters import PyPISimpleAdapter, WarehouseAdapter, simple
from app.ecosystems.pypi.adapters.simple import (
    ACCEPT,
    MetadataIntegrityError,
    group_versions,
    parse_html_page,
    pick_metadata_file,
)
from app.ecosystems.pypi.connector import PyPIConnector
from app.ecosystems.registry import get_plugin, load_builtin_plugins

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "pypi_simple"
CPU = "https://download.pytorch.org/whl/cpu/"
CU121 = "https://download.pytorch.org/whl/cu121/"
TORCH_CPU_PAGE = f"{CPU}torch/"
WHEEL_HOST = "https://download-r2.pytorch.org/whl/cpu/"
LINUX_WHEEL = "torch-2.9.1%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl"
WINDOWS_WHEEL = "torch-2.9.1%2Bcpu-cp312-cp312-win_amd64.whl"
ETAG = '"51a4855153fddfaae49c034cf1e551bf"'
NO_LOOKUP = None  # the adapter does not use the lookup


def _registry(base_url: str = CPU, **config: object) -> RegistryConfig:
    return RegistryConfig(
        id="reg-torch-cpu", slug="pytorch-cpu", ecosystem="pypi", protocol="pypi-simple", base_url=base_url,
        priority=10, config=config, routing=None, auth_type="none", credentials_ref=None,
    )


def _ref(name: str) -> PackageRef:
    return PackageRef(ecosystem="pypi", namespace="", name=name)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _html(text: str, etag: str | None = ETAG) -> httpx.Response:
    headers = {"content-type": "text/html"}
    if etag:
        headers["etag"] = etag
    return httpx.Response(200, text=text, headers=headers)


class Index:
    """A fake upstream: URL -> response (or a callable), every request is kept."""

    def __init__(self, routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404, text="not found")
        return route(request) if callable(route) else route

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


@pytest.fixture
def serve(monkeypatch):
    def start(routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> Index:
        index = Index(routes)
        monkeypatch.setattr(
            simple, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(index.handler))
        )
        return index

    return start


def _adapter(registry: RegistryConfig | None = None) -> PyPISimpleAdapter:
    adapter = PyPISimpleAdapter(registry or _registry())
    adapter.max_retries = 0   # a 5xx in a test is an answer, not something to wait out
    return adapter


def _fetch(adapter: PyPISimpleAdapter, name: str):
    return asyncio.run(adapter.fetch_package(_ref(name), NO_LOOKUP))  # type: ignore[arg-type]


def _deps(adapter: PyPISimpleAdapter, name: str, version: str):
    return asyncio.run(adapter.fetch_version_dependencies(_ref(name), version, NO_LOOKUP))  # type: ignore[arg-type]


def _metadata_routes(wheel_host: str = WHEEL_HOST) -> dict[str, httpx.Response]:
    return {
        f"{wheel_host}{LINUX_WHEEL}.metadata": httpx.Response(200, text=_text("torch-2.9.1+cpu-linux_x86_64.metadata")),
        f"{wheel_host}{WINDOWS_WHEEL}.metadata": httpx.Response(200, text=_text("torch-2.9.1+cpu-win_amd64.metadata")),
    }


# --- the plugin ----------------------------------------------------------------------------------------------


def test_plugin_gives_the_simple_adapter_to_pypi_simple_registries() -> None:
    load_builtin_plugins()
    assert isinstance(get_plugin("pypi").adapter(_registry()), PyPISimpleAdapter)
    assert _adapter().min_interval_seconds() == 0.0


def test_project_page_is_under_the_root_or_the_configured_simple_url() -> None:
    assert _adapter(_registry("https://download.pytorch.org/whl/cu121")).page_url(_ref("torch")) == f"{CU121}torch/"
    override = _registry("https://devpi.example/root/pypi", simple_url="https://devpi.example/root/pypi/+simple")
    assert _adapter(override).page_url(_ref("my-pkg")) == "https://devpi.example/root/pypi/+simple/my-pkg/"


# --- HTML (PEP 503) ------------------------------------------------------------------------------------------


def test_cu121_page_without_metadata(serve) -> None:
    index = serve({f"{CU121}torch/": _html(_text("cu121-torch.html"))})
    package = _fetch(_adapter(_registry(CU121)), "torch")

    assert index.urls == [f"{CU121}torch/"]
    assert index.requests[0].headers["accept"] == ACCEPT
    assert package is not None
    assert (package.display_name, package.registry_id, package.revision) == ("torch", "reg-torch-cpu", ETAG)
    assert package.description is None and package.authors is None and package.vulnerabilities is None
    assert [v.version for v in package.versions] == ["2.5.0+cu121", "2.5.1+cu121"]

    latest = package.versions[1]
    assert latest.status == "active" and latest.dependencies is None and latest.engines is None
    assert latest.release_date == datetime(2024, 10, 29, 23, 15, 46, tzinfo=UTC)   # the earliest of its files
    assert [a.filename for a in latest.artifacts] == [
        "torch-2.5.1+cu121-cp310-cp310-linux_x86_64.whl",
        "torch-2.5.1+cu121-cp310-cp310-win_amd64.whl",
        "torch-2.5.1+cu121-cp311-cp311-linux_x86_64.whl",
    ]
    assert not any(a.has_metadata_file for a in latest.artifacts)


def test_cpu_page_local_and_public_versions_on_another_host(serve) -> None:
    serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html"))})
    package = _fetch(_adapter(), "torch")

    assert package is not None
    versions = {v.version: v for v in package.versions}
    assert list(versions) == ["2.1.0+cpu", "2.9.1+cpu", "2.9.1"]   # `2.9.1` is a version of its own

    local = versions["2.9.1+cpu"]
    linux = next(a for a in local.artifacts if a.filename.endswith("cp312-cp312-manylinux_2_28_x86_64.whl"))
    assert linux.filename == "torch-2.9.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl"   # %2B decoded
    assert linux.download_url == f"{WHEEL_HOST}{LINUX_WHEEL}"                           # other host, no fragment
    assert linux.artifact_type == "wheel" and linux.has_metadata_file
    assert linux.checksums == {"sha256": "7417d8c565f219d3455654cb431c6d892a3eb40246055e14d645422de13b9ea1"}
    assert linux.platform_tags == {"python": ["cp312"], "abi": ["cp312"], "platform": ["manylinux_2_28_x86_64"]}
    assert local.release_date == datetime(2026, 1, 23, 15, 12, 51, tzinfo=UTC)

    assert not any(a.has_metadata_file for a in versions["2.1.0+cpu"].artifacts)
    assert versions["2.9.1"].artifacts[0].platform_tags == {
        "python": ["cp312"], "abi": ["none"], "platform": ["macosx_11_0_arm64"]
    }


def test_attributes_are_unescaped_and_yanked_follows_all_files(serve) -> None:
    serve({f"{CPU}filelock/": _html(_text("cpu-filelock.html"))})
    package = _fetch(_adapter(), "filelock")

    assert package is not None
    versions = {v.version: v for v in package.versions}
    assert list(versions) == ["0.2.0", "3.0.12", "3.15.0", "3.15.1"]
    assert (versions["3.15.0"].status, versions["3.15.0"].status_reason) == ("yanked", "broken release")
    assert (versions["3.15.1"].status, versions["3.15.1"].status_reason) == ("active", None)
    assert versions["3.15.1"].engines == {"python": ">=3.8"}   # `&gt;=3.8` in the page
    assert versions["0.2.0"].engines is None and versions["0.2.0"].artifacts[0].artifact_type == "sdist"
    assert versions["0.2.0"].artifacts[0].platform_tags is None
    assert [a.has_metadata_file for a in versions["3.15.0"].artifacts] == [True, False]


PAGE = "https://idx.example/simple/demo/"


def _anchor(filename: str, attributes: str = "", href: str | None = None) -> str:
    return f'<a href="{href or filename}" {attributes}>{filename}</a><br/>'


def test_version_is_yanked_only_if_every_file_is() -> None:
    files = parse_html_page(
        "\n".join([
            _anchor("demo-1.0-py3-none-any.whl", 'data-yanked=""'),
            _anchor("demo-1.0.tar.gz", 'data-yanked="oops &amp; more"'),
            _anchor("demo-2.0-py3-none-any.whl", "data-yanked"),
            _anchor("demo-2.0.tar.gz"),
        ]),
        PAGE,
    )
    versions = {version: simple._version(version, parsed) for version, parsed in group_versions(_ref("demo"), files).items()}
    assert (versions["1.0"].status, versions["1.0"].status_reason) == ("yanked", "oops & more")
    assert versions["2.0"].status == "active"


def test_engines_come_from_the_files_that_agree_or_the_most_common() -> None:
    files = parse_html_page(
        "\n".join([
            _anchor("demo-1.0-py3-none-any.whl", 'data-requires-python="&gt;=3.8"'),
            _anchor("demo-1.0.tar.gz", 'data-requires-python="&gt;=3.8"'),
            _anchor("demo-2.0-cp310-cp310-linux_x86_64.whl", 'data-requires-python="&gt;=3.10"'),
            _anchor("demo-2.0-cp311-cp311-linux_x86_64.whl", 'data-requires-python="&gt;=3.9"'),
            _anchor("demo-2.0-cp312-cp312-linux_x86_64.whl", 'data-requires-python="&gt;=3.9"'),
            _anchor("demo-3.0.tar.gz"),
        ]),
        PAGE,
    )
    versions = {v: simple._version(v, parsed) for v, parsed in group_versions(_ref("demo"), files).items()}
    assert versions["1.0"].engines == {"python": ">=3.8"}
    assert versions["2.0"].engines == {"python": ">=3.9"}
    assert versions["3.0"].engines is None


def test_files_of_other_projects_and_unparsable_files_are_skipped() -> None:
    files = parse_html_page(
        "\n".join([
            _anchor("Demo_Pkg-1.0-py3-none-any.whl"),
            _anchor("demo.pkg-1.0.tar.gz"),
            _anchor("other-1.0-py3-none-any.whl"),
            _anchor("demo_pkg-1.0-py2.7.egg"),
            _anchor("demo_pkg-1.0.win32.exe"),
            _anchor("demo_pkg-not-a-version.tar.gz"),
            _anchor("garbage.whl"),
            '<a name="no-href">anchor</a>',
        ]),
        PAGE,
    )
    versions = group_versions(_ref("demo-pkg"), files)
    assert {v: sorted(p.file.filename for p in parsed) for v, parsed in versions.items()} == {
        "1.0": ["Demo_Pkg-1.0-py3-none-any.whl", "demo.pkg-1.0.tar.gz"]
    }


def test_hrefs_are_resolved_against_the_page_and_the_version_is_the_one_in_the_file_name() -> None:
    files = parse_html_page(
        _anchor("demo-2.9.1%2Bcpu-py3-none-any.whl", 'data-core-metadata="true"', href="../files/demo-2.9.1%2Bcpu-py3-none-any.whl#md5=abc"),
        "https://idx.example/simple/demo/",
    )
    assert files[0].url == "https://idx.example/simple/files/demo-2.9.1%2Bcpu-py3-none-any.whl"
    assert files[0].hashes == {"md5": "abc"} and files[0].has_metadata and files[0].metadata_hashes == {}
    assert list(group_versions(_ref("demo"), files)) == ["2.9.1+cpu"]
    # `1.0.0` stays as written: it is a different string than `1.0`, as on pypi.org.
    assert list(group_versions(_ref("demo"), parse_html_page(_anchor("demo-1.0.0.tar.gz"), PAGE))) == ["1.0.0"]


def test_old_spelling_of_the_metadata_attribute_is_understood() -> None:
    (file,) = parse_html_page(_anchor("demo-1.0-py3-none-any.whl", 'data-dist-info-metadata="sha256=ABC"'), PAGE)
    assert file.has_metadata and file.metadata_hashes == {"sha256": "abc"}


# --- JSON (PEP 691) ------------------------------------------------------------------------------------------


def _json(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "application/vnd.pypi.simple.v1+json", "etag": '"j1"'})


def test_pep691_json_page(serve) -> None:
    serve({"https://idx.example/simple/mpmath/": _json(_text("pypi-mpmath.json"))})
    package = _fetch(_adapter(_registry("https://idx.example/simple/")), "mpmath")

    assert package is not None and package.revision == '"j1"'
    versions = {v.version: v for v in package.versions}
    assert list(versions) == ["1.3.0", "1.4.1"]
    wheel, sdist = versions["1.4.1"].artifacts
    assert (wheel.filename, wheel.artifact_type, wheel.has_metadata_file) == ("mpmath-1.4.1-py3-none-any.whl", "wheel", True)
    assert (sdist.artifact_type, sdist.has_metadata_file, sdist.size_bytes) == ("sdist", False, 2093211)
    assert (wheel.size_bytes, wheel.checksums) == (
        567787, {"sha256": "dc4f0ea2304480d4a9a48a94c1020571558ade522b44a6912efac63a586e140f"})
    assert versions["1.4.1"].engines == {"python": ">=3.9"} and versions["1.3.0"].engines is None
    assert versions["1.4.1"].release_date == datetime(2026, 3, 15, 1, 17, 36, 392621, tzinfo=UTC)


def test_pep691_yanked_and_metadata_hashes() -> None:
    page = {
        "name": "demo",
        "files": [
            {"filename": "demo-1.0-py3-none-any.whl", "url": "demo-1.0-py3-none-any.whl", "hashes": {"sha256": "AA"},
             "yanked": "bad build", "core-metadata": {"sha256": "BB"}, "upload-time": "2025-01-02T03:04:05Z", "size": 12},
            {"filename": "demo-1.0.tar.gz", "url": "demo-1.0.tar.gz", "hashes": {}, "yanked": True,
             "dist-info-metadata": True},
            {"filename": "demo-2.0.tar.gz", "url": "demo-2.0.tar.gz", "hashes": {}, "yanked": False},
        ],
    }
    files = simple.parse_json_page(page, PAGE)
    assert files[0].url == f"{PAGE}demo-1.0-py3-none-any.whl"
    assert files[0].hashes == {"sha256": "aa"} and files[0].metadata_hashes == {"sha256": "bb"} and files[0].size == 12
    assert files[0].upload_time == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert files[1].has_metadata and files[1].yanked_reason is None and not files[2].yanked
    versions = {v: simple._version(v, p) for v, p in group_versions(_ref("demo"), files).items()}
    assert (versions["1.0"].status, versions["1.0"].status_reason) == ("yanked", "bad build")
    assert versions["2.0"].status == "active"


# --- existence and the connection ----------------------------------------------------------------------------


def test_missing_project_is_none_and_does_not_exist(serve) -> None:
    serve({})
    adapter = _adapter()
    assert _fetch(adapter, "nope") is None
    assert asyncio.run(adapter.exists(_ref("nope"))) is False


def test_exists_is_true_for_a_page_and_other_errors_raise(serve) -> None:
    serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html")), f"{CPU}broken/": httpx.Response(500)})
    adapter = _adapter()
    assert asyncio.run(adapter.exists(_ref("torch"))) is True
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.exists(_ref("broken")))
    with pytest.raises(httpx.HTTPStatusError):
        _fetch(adapter, "broken")


def test_transient_errors_are_retried(serve, monkeypatch) -> None:
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else _html(_text("cpu-torch.html"))

    serve({TORCH_CPU_PAGE: flaky})

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(simple.asyncio, "sleep", no_sleep)
    adapter = PyPISimpleAdapter(_registry())
    adapter.max_retries = 2
    assert _fetch(adapter, "torch") is not None and len(calls) == 2


def test_connection_check_reads_the_root(serve) -> None:
    index = serve({CPU: _html("<html><body></body></html>")})
    asyncio.run(_adapter().test_connection())
    assert index.urls == [CPU]


def test_connection_check_fails_for_a_wrong_root_or_an_unauthorized_source(serve) -> None:
    serve({f"{CPU}": httpx.Response(401)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())
    serve({})   # 404 root: base_url is not an index
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())


# --- dependencies from PEP 658 -------------------------------------------------------------------------------


def test_metadata_of_the_manylinux_wheel_with_the_newest_cpython(serve) -> None:
    index = serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html")), **_metadata_routes()})
    result = _deps(_adapter(), "torch", "2.9.1+cpu")

    assert index.urls == [TORCH_CPU_PAGE, f"{WHEEL_HOST}{LINUX_WHEEL}.metadata"]   # cp312 over cp310, Linux over Windows
    assert result.available and result.engines == {"python": ">=3.10"}
    by_name = {(d.target.name, d.extra_name): d for d in result.dependencies}
    assert not any(name.startswith("nvidia") for name, _ in by_name)
    assert {name for name, extra in by_name if extra is None} == {
        "filelock", "typing-extensions", "setuptools", "sympy", "networkx", "jinja2", "fsspec"}
    assert by_name[("typing-extensions", None)].constraint == ">=4.10.0"
    assert by_name[("setuptools", None)].environment_markers == {
        "type": "python_environment", "expression": 'python_version >= "3.12"',
        "conditions": [{"marker": "python_version", "op": ">=", "value": "3.12"}],
    }
    assert by_name[("optree", "optree")].dependency_type == "optional"

    (document,) = result.documents
    assert document.path == f"{WHEEL_HOST}{LINUX_WHEEL}.metadata"   # another host: the full URL
    assert document.content == (FIXTURES / "torch-2.9.1+cpu-linux_x86_64.metadata").read_bytes()
    assert (document.content_type, document.etag) == ("text/plain", None)


def test_document_path_is_relative_to_the_base_url_when_it_starts_with_it(serve) -> None:
    serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html")), **_metadata_routes()})
    # The files of this index live under the registry's base_url, the project pages under `simple_url`.
    registry = _registry("https://download-r2.pytorch.org/whl/cpu", simple_url=CPU)
    result = _deps(_adapter(registry), "torch", "2.9.1+cpu")
    assert [d.path for d in result.documents] == [f"{LINUX_WHEEL}.metadata"]


def test_dependencies_equal_what_the_warehouse_path_gives_for_the_same_requires_dist(serve, monkeypatch) -> None:
    from app.ecosystems.pypi.adapters import warehouse
    from app.ecosystems.pypi import connector as connector_module

    serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html")), **_metadata_routes()})
    from_index = _deps(_adapter(), "torch", "2.9.1+cpu")

    metadata = (FIXTURES / "torch-2.9.1+cpu-linux_x86_64.metadata").read_text()
    requires_dist = [line.split(": ", 1)[1] for line in metadata.splitlines() if line.startswith("Requires-Dist: ")]
    payload = {"info": {"requires_dist": requires_dist, "requires_python": ">=3.10"}}
    monkeypatch.setattr(
        connector_module, "make_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))),
    )
    adapter = WarehouseAdapter(_registry("https://pypi.invalid/"), connector=PyPIConnector(json_base_url="https://pypi.invalid/pypi"))
    from_warehouse = asyncio.run(adapter.fetch_version_dependencies(_ref("torch"), "2.9.1", NO_LOOKUP))  # type: ignore[arg-type]

    assert warehouse.dependencies_from_requires_dist(requires_dist) == from_index.dependencies
    assert from_index.dependencies == from_warehouse.dependencies and len(from_index.dependencies) == 10
    assert from_index.engines == from_warehouse.engines


def test_a_hash_mismatch_is_a_source_error(serve) -> None:
    routes = {TORCH_CPU_PAGE: _html(_text("cpu-torch.html")), **_metadata_routes()}
    routes[f"{WHEEL_HOST}{LINUX_WHEEL}.metadata"] = httpx.Response(200, text="Metadata-Version: 2.1\nName: torch\n")
    serve(routes)
    with pytest.raises(MetadataIntegrityError, match="published"):
        _deps(_adapter(), "torch", "2.9.1+cpu")


def test_the_published_hash_matches_the_fixture_metadata() -> None:
    page = _text("cpu-torch.html")
    for name, wheel in (("linux_x86_64", LINUX_WHEEL), ("win_amd64", WINDOWS_WHEEL)):
        digest = hashlib.sha256((FIXTURES / f"torch-2.9.1+cpu-{name}.metadata").read_bytes()).hexdigest()
        anchor = next(line for line in page.splitlines() if wheel in line)
        assert f'data-core-metadata="sha256={digest}"' in anchor


def test_version_without_metadata_is_unavailable_not_an_error(serve) -> None:
    index = serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html"))})
    adapter = _adapter()
    for version in ("2.1.0+cpu", "0.0.1"):
        result = _deps(adapter, "torch", version)
        assert result.available is False and result.dependencies == [] and result.documents == ()
    assert all(url == TORCH_CPU_PAGE for url in index.urls)   # no .metadata was asked for


def test_missing_project_page_gives_unavailable_dependencies(serve) -> None:
    serve({})
    assert _deps(_adapter(), "torch", "2.9.1+cpu").available is False


def test_missing_metadata_file_that_the_page_promised_is_an_error(serve) -> None:
    serve({TORCH_CPU_PAGE: _html(_text("cpu-torch.html"))})
    with pytest.raises(httpx.HTTPStatusError):
        _deps(_adapter(), "torch", "2.9.1+cpu")


def test_metadata_file_choice() -> None:
    def parsed(*names: str):
        files = parse_html_page(
            "\n".join(_anchor(n, 'data-core-metadata="true"') for n in names), "https://x.example/p/")
        return [p for ps in group_versions(_ref("demo"), files).values() for p in ps]

    def chosen(*names: str) -> str | None:
        file = pick_metadata_file(parsed(*names))
        return file.file.filename if file else None

    assert chosen(
        "demo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl", "demo-1.0-py3-none-any.whl", "demo-1.0-cp313-cp313-win_amd64.whl"
    ) == "demo-1.0-py3-none-any.whl"
    assert chosen(
        "demo-1.0-cp39-cp39-manylinux2014_x86_64.whl", "demo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
        "demo-1.0-cp313-cp313-manylinux_2_28_aarch64.whl", "demo-1.0-cp313-cp313-win_amd64.whl",
    ) == "demo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"   # cp311 over cp39 (not a string comparison)
    assert chosen("demo-1.0-cp313-cp313-win_amd64.whl", "demo-1.0-cp310-cp310-win_amd64.whl") == "demo-1.0-cp310-cp310-win_amd64.whl"
    assert chosen() is None

    without = parse_html_page(_anchor("demo-1.0-py3-none-any.whl"), PAGE)
    assert pick_metadata_file([p for ps in group_versions(_ref("demo"), without).values() for p in ps]) is None


# --- change detection by ETag --------------------------------------------------------------------------------


def _tracked(name: str, revision: str | None, *, etag: str | None = None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(ref=_ref(name), package_id=name, revision=revision, etag=etag, unavailable=unavailable)


def _conditional(etag: str, new_etag: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == etag and new_etag is None:
            return httpx.Response(304, headers={"etag": etag})
        return _html("<html></html>", etag=new_etag or etag)

    return answer


def test_detector_asks_conditionally_and_reports_changed_and_missing(serve) -> None:
    index = serve({
        f"{CPU}same/": _conditional('"a"'),
        f"{CPU}changed/": _conditional('"old"', new_etag='"new"'),
        f"{CPU}gone/": httpx.Response(404),
        f"{CPU}back/": _conditional('"b"'),
        f"{CPU}still-gone/": httpx.Response(404),
    })
    tracked = [
        _tracked("same", '"a"'), _tracked("changed", '"old"'), _tracked("gone", '"g"'), _tracked("never-synced", None),
        _tracked("back", '"b"', unavailable=True), _tracked("still-gone", '"s"', unavailable=True),
        _tracked("from-etag-column", None, etag='"a"'),
    ]
    index.routes[f"{CPU}from-etag-column/"] = _conditional('"a"')

    result = asyncio.run(_adapter().change_detector().detect(tracked, {"anything": 1}))

    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("changed", '"new"'), ("never-synced", None), ("back", '"b"')
    ]
    assert [p.ref.name for p in result.missing] == ["gone"]
    assert result.cursor == {} and not result.reconcile_all
    sent = {str(r.url).rsplit("/", 2)[-2]: r.headers.get("if-none-match") for r in index.requests}
    assert sent["same"] == '"a"' and sent["changed"] == '"old"' and sent["from-etag-column"] == '"a"'
    assert "never-synced" not in sent   # without a stored ETag there is nothing to ask conditionally


def test_detector_bounds_the_concurrency(monkeypatch) -> None:
    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(304, headers={"etag": '"a"'})

    monkeypatch.setattr(simple, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    detector = simple.SimpleChangeDetector(_adapter(), concurrency=3)
    result = asyncio.run(detector.detect([_tracked(f"p{i}", '"a"') for i in range(8)], {}))

    assert result.changed == [] and result.missing == [] and peak == 3


def test_versions_are_grouped_by_their_pep440_normal_form() -> None:
    files = parse_html_page(
        _anchor("demo-1.0.0.RC1.tar.gz") + _anchor("demo-1.0.0rc1-py3-none-any.whl"), PAGE)
    assert {version: len(parsed) for version, parsed in group_versions(_ref("demo"), files).items()} == {"1.0.0rc1": 2}


def test_pep691_empty_yank_reason_is_still_yanked() -> None:
    page = {"name": "demo", "files": [
        {"filename": "demo-1.0.tar.gz", "url": "demo-1.0.tar.gz", "hashes": {}, "yanked": ""}]}
    (file,) = simple.parse_json_page(page, PAGE)
    assert (file.yanked, file.yanked_reason) == (True, None)


def test_metadata_url_goes_before_a_query_string(serve) -> None:
    signed = "https://files.example/demo-1.0-py3-none-any.whl?token=abc"
    page = _anchor("demo-1.0-py3-none-any.whl", 'data-core-metadata="true"', href=signed)
    index = serve({PAGE: _html(page),
                   "https://files.example/demo-1.0-py3-none-any.whl.metadata?token=abc":
                       httpx.Response(200, content=b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nRequires-Dist: idna\n")})
    result = _deps(_adapter(_registry("https://idx.example/simple/")), "demo", "1.0")
    assert [d.target.name for d in result.dependencies] == ["idna"]
    assert index.urls[-1] == "https://files.example/demo-1.0-py3-none-any.whl.metadata?token=abc"


def test_detector_survives_one_failing_page(serve) -> None:
    serve({f"{CPU}changed/": _conditional('"old"', new_etag='"new"'), f"{CPU}broken/": httpx.Response(503)})
    detector = _adapter().change_detector()
    result = asyncio.run(detector.detect([_tracked("changed", '"old"'), _tracked("broken", '"b"')], {}))
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("changed", '"new"')]
    assert result.missing == []
