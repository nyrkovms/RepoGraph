"""The `goproxy` adapter (WP H3) on answers recorded from proxy.golang.org on 2026-10-30 with
`curl -A "PackageHub/0.1 (+https://github.com/nyrkovms/RepoGraph)"` (tests/fixtures/go/goproxy/<escaped path>/...):

- github.com/gin-gonic/gin: the full `@v/list` (28 versions), `@latest` (v1.12.0), `.mod` of v1.10.0 and v1.12.0 and
  `.info` of only four versions (v1.12.0, v1.10.0, v1.9.1, v1.7.7): the other 24 answer 404 here, which is how the
  tests see a version whose date could not be read;
- github.com/redis/go-redis/v9: `@v/list`, `@latest` (v9.23.0) whose go.mod has a `retract` block with single versions
  and rationales; `.info` of some of the versions;
- github.com/mattn/go-sqlite3: `@v/list` with `+incompatible` versions, `@latest` v1.14.52 whose go.mod retracts the
  interval [v2.0.0+incompatible, v2.0.7+incompatible];
- golang.org/x/exp: an empty `@v/list` and a pseudo-version as `@latest` (the module has no tags);
- github.com/docker/docker: `+incompatible` versions only, a go.mod that is just the `module` line;
- github.com/golang/protobuf: a go.mod with `// Deprecated:` above `module`.
Files were recorded whole; nothing was trimmed except by not recording more `.info`. Everything is served by a fake
upstream; nothing here reaches the network."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.ecosystems.base import FetchedDocument, NormalizedPackage, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.go.adapters import GoListChangeDetector, GoProxyAdapter
from app.ecosystems.go.adapters import goproxy
from app.ecosystems.go.adapters.goproxy import list_revision, retraction
from app.ecosystems.go.coordinates import GoCoordinates
from app.ecosystems.go.gomod import parse_go_mod

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "go" / "goproxy"
BASE = "https://proxy.golang.org/"
NO_LOOKUP = None  # the adapter does not use the lookup
Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


def _registry(base_url: str = BASE) -> RegistryConfig:
    return RegistryConfig(
        id="reg-go", slug="goproxy", ecosystem="go", protocol="goproxy", base_url=base_url,
        priority=0, config={}, routing=None, auth_type="none", credentials_ref=None,
    )


def _ref(raw: str) -> PackageRef:
    return GoCoordinates().parse(raw)


def _recorded(path: str) -> str:
    return (FIXTURES / path).read_text()


def _recorded_versions(module: str) -> list[str]:
    return [line for line in _recorded(f"{module}/@v/list").splitlines() if line]


class Upstream:
    """A fake proxy: explicit routes first, then the recorded files under the same path, else 404."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is not None:
            return route(request) if callable(route) else route
        path = request.url.path.lstrip("/")
        if request.url.host == "proxy.golang.org" and (FIXTURES / path).is_file():
            return httpx.Response(200, text=(FIXTURES / path).read_text())
        return httpx.Response(404, text="not found: module lookup failed")

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


@pytest.fixture
def serve(monkeypatch):
    def start(routes: dict[str, Route] | None = None) -> Upstream:
        upstream = Upstream(routes or {})
        monkeypatch.setattr(
            goproxy, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
        )
        return upstream

    return start


def _adapter(base_url: str = BASE) -> GoProxyAdapter:
    adapter = GoProxyAdapter(_registry(base_url))
    adapter.max_retries = 0   # a 5xx in a test is an answer, not something to wait out
    return adapter


def _fetch(raw: str, adapter: GoProxyAdapter | None = None) -> NormalizedPackage | None:
    return asyncio.run((adapter or _adapter()).fetch_package(_ref(raw), NO_LOOKUP))  # type: ignore[arg-type]


def _version(package: NormalizedPackage, number: str):
    return next(v for v in package.versions if v.version == number)


def _answer(url: str, text: str, **headers: str) -> dict[str, Route]:
    return {url: httpx.Response(200, text=text, headers=headers)}


# --- fetch_package: gin -----------------------------------------------------------------------------------------


def test_gin_versions_dates_and_revision(serve) -> None:
    upstream = serve()
    package = _fetch("github.com/gin-gonic/gin")
    assert package is not None
    listed = _recorded_versions("github.com/gin-gonic/gin")
    assert sorted(v.version for v in package.versions) == sorted(listed) and len(listed) == 28
    assert (package.display_name, package.registry_id) == ("github.com/gin-gonic/gin", "reg-go")
    assert package.revision == hashlib.sha256("\n".join(sorted(listed)).encode()).hexdigest()
    assert (package.description, package.license_raw, package.license_spdx, package.authors) == (None, None, None, None)
    assert package.deprecation is None

    # Regression (#43): only the latest is dated, from `@latest`; a `.info` per version made adding a module slow.
    dated = {v.version for v in package.versions if v.release_date is not None}
    assert dated == {"v1.12.0"} and not [url for url in upstream.urls if url.endswith(".info")]


def test_gin_release_dates_come_from_info(serve) -> None:
    serve()
    expected = json.loads(_recorded("github.com/gin-gonic/gin/@v/v1.7.7.info"))["Time"]
    found = asyncio.run(_adapter().release_date(_ref("github.com/gin-gonic/gin"), "v1.7.7"))
    assert found == datetime.fromisoformat(expected) == datetime(2021, 11, 24, 13, 54, 13, tzinfo=UTC)


def test_gin_only_the_latest_version_has_dependencies(serve) -> None:
    upstream = serve()
    package = _fetch("github.com/gin-gonic/gin")
    assert package is not None
    latest = _version(package, "v1.12.0")
    assert latest.dependencies is not None and len(latest.dependencies) == 35
    assert {v.version for v in package.versions if v.dependencies is not None} == {"v1.12.0"}
    mod_requests = [url for url in upstream.urls if url.endswith(".mod")]
    assert mod_requests == [f"{BASE}github.com/gin-gonic/gin/@v/v1.12.0.mod"]

    by_path = {d.target.name: d for d in latest.dependencies}
    sonic = by_path["github.com/bytedance/sonic"]
    assert (sonic.constraint, sonic.dependency_type, sonic.scope, sonic.raw_spec, sonic.details, sonic.alias) == (
        "v1.15.0", "runtime", "require", "github.com/bytedance/sonic v1.15.0", None, None)
    assert sonic.target.ecosystem == "go" and sonic.target.namespace == ""
    yaml = by_path["gopkg.in/yaml.v3"]
    assert (yaml.raw_spec, yaml.details, yaml.dependency_type) == (
        "gopkg.in/yaml.v3 v3.0.1 // indirect", {"indirect": True}, "runtime")
    assert latest.engines == {"go": "1.25.0"}
    assert latest.raw_metadata == {"go_mod": {"go": "1.25.0"}}
    assert _version(package, "v1.10.0").engines is None and _version(package, "v1.10.0").raw_metadata == {}


def test_gin_artifacts(serve) -> None:
    serve()
    package = _fetch("github.com/gin-gonic/gin")
    assert package is not None
    [artifact] = _version(package, "v1.10.0").artifacts
    assert (artifact.filename, artifact.artifact_type, artifact.checksums) == ("gin@v1.10.0.zip", "zip", {})
    assert artifact.download_url == f"{BASE}github.com/gin-gonic/gin/@v/v1.10.0.zip"
    assert all(len(v.artifacts) == 1 for v in package.versions)


def test_gin_statuses_are_active_without_retract(serve) -> None:
    serve()
    package = _fetch("github.com/gin-gonic/gin")
    assert package is not None
    assert {(v.status, v.status_reason) for v in package.versions} == {("active", None)}


# --- retract, deprecated ----------------------------------------------------------------------------------------


def test_retract_block_marks_the_listed_versions_yanked_with_the_rationale(serve) -> None:
    serve()
    package = _fetch("github.com/redis/go-redis/v9")
    assert package is not None
    assert (package.display_name, package.revision) == ("github.com/redis/go-redis/v9", list_revision(
        _recorded_versions("github.com/redis/go-redis/v9")))
    yanked = {v.version: v.status_reason for v in package.versions if v.status == "yanked"}
    assert yanked == {
        "v9.15.1": "This version is used to retract v9.15.0",
        "v9.15.0": "This version was accidentally released. It is identical to 9.15.0-beta.2",
        "v9.7.2": "This version was accidentally released. Please use version 9.7.3 instead.",
        "v9.5.4": "This version was accidentally released. Please use version 9.6.0 instead.",
        "v9.5.3": "This version was accidentally released. Please use version 9.6.0 instead.",
    }
    assert _version(package, "v9.6.0").status == "active"
    assert _version(package, "v9.15.0-beta.2").status == "active"   # not covered: a prerelease of the retracted number


def test_retract_block_the_latest_go_mod_gives_the_dependencies(serve) -> None:
    serve()
    package = _fetch("github.com/redis/go-redis/v9")
    assert package is not None
    latest = _version(package, "v9.23.0")
    assert latest.dependencies is not None
    assert [d.raw_spec for d in latest.dependencies if d.details] == ["github.com/klauspost/cpuid/v2 v2.4.0 // indirect"]
    assert latest.engines == {"go": "1.26.0"}
    assert asyncio.run(_adapter().release_date(_ref("github.com/redis/go-redis/v9"), "v9.5.3")) == \
        datetime.fromisoformat(json.loads(_recorded("github.com/redis/go-redis/v9/@v/v9.5.3.info"))["Time"])


def test_retract_interval_covers_incompatible_versions_inclusively() -> None:
    go_mod = parse_go_mod(_recorded("github.com/mattn/go-sqlite3/@v/v1.14.52.mod"))
    covered = {v for v in _recorded_versions("github.com/mattn/go-sqlite3") if retraction(go_mod, v)}
    assert covered == {f"v2.0.{n}+incompatible" for n in range(0, 4)}   # the list has v2.0.0 to v2.0.3
    assert {retraction(go_mod, v) for v in covered} == {"Accidental; no major changes or features."}
    assert retraction(go_mod, "v1.14.0") is None and retraction(go_mod, "v2.0.8+incompatible") is None


def test_the_latest_is_what_the_proxy_says_not_an_incompatible_tag_above_it(serve) -> None:
    """go-sqlite3 has v2.0.x+incompatible tags above its real latest v1.14.52 (what `go get @latest` takes and the
    proxy's `@latest` names); the go.mod of v1.14.52 retracts them."""
    upstream = serve()
    package = _fetch("github.com/mattn/go-sqlite3")
    assert package is not None
    assert [url for url in upstream.urls if url.endswith(".mod")] == [f"{BASE}github.com/mattn/go-sqlite3/@v/v1.14.52.mod"]
    retracted = {v.version for v in package.versions if v.status == "yanked"}
    assert retracted and all(v.startswith("v2.0.") and v.endswith("+incompatible") for v in retracted)
    assert _version(package, "v1.14.52").dependencies is not None


def test_an_interval_retract_marks_every_listed_version_in_it(serve) -> None:
    mod = "module example.com/m\n\nretract [v2.0.0+incompatible, v2.0.7+incompatible] // Accidental\n"
    serve({
        **_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\nv2.0.0+incompatible\nv2.0.3+incompatible\nv2.0.8+incompatible\nv1.5.0\n"),
        **_answer(f"{BASE}example.com/m/@v/v2.0.8+incompatible.mod", mod),
    })
    package = _fetch("example.com/m")
    assert package is not None
    assert {v.version: v.status for v in package.versions} == {
        "v1.0.0": "active", "v1.5.0": "active", "v2.0.0+incompatible": "yanked", "v2.0.3+incompatible": "yanked",
        "v2.0.8+incompatible": "active"}
    assert {v.status_reason for v in package.versions if v.status == "yanked"} == {"Accidental"}


def test_a_retract_without_rationale_says_retracted(serve) -> None:
    mod = "module example.com/m\n\nretract (\n\tv1.0.0\n\t[v1.2.0, v1.3.0]\n)\n"
    serve({
        **_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\nv1.1.0\nv1.2.5\nv1.3.0\nv1.4.0\n"),
        **_answer(f"{BASE}example.com/m/@v/v1.4.0.mod", mod),
    })
    package = _fetch("example.com/m")
    assert package is not None
    assert {v.version: (v.status, v.status_reason) for v in package.versions} == {
        "v1.0.0": ("yanked", "retracted"), "v1.1.0": ("active", None), "v1.2.5": ("yanked", "retracted"),
        "v1.3.0": ("yanked", "retracted"), "v1.4.0": ("active", None)}


def test_deprecated_go_mod_deprecates_the_package(serve) -> None:
    serve()
    package = _fetch("github.com/golang/protobuf")
    assert package is not None
    assert package.deprecation == {"message": 'Use the "google.golang.org/protobuf" module instead.'}
    assert {v.deprecation for v in package.versions} == {None}
    latest = _version(package, "v1.5.4")
    assert latest.dependencies is not None and [d.target.name for d in latest.dependencies] == [
        "github.com/google/go-cmp", "google.golang.org/protobuf"]
    assert latest.engines == {"go": "1.17"}


# --- no tags, +incompatible, odd lists --------------------------------------------------------------------------


def test_a_module_without_tags_has_its_latest_pseudo_version_as_the_only_version(serve) -> None:
    upstream = serve()
    package = _fetch("golang.org/x/exp")
    assert package is not None
    [version] = package.versions
    assert version.version == "v0.0.0-20261014143641-f0251eb4aa3e"
    assert version.release_date == datetime(2026, 10, 14, 14, 36, 41, tzinfo=UTC)
    assert version.status == "active"
    assert package.revision == "latest:v0.0.0-20261014143641-f0251eb4aa3e"   # a new commit changes it
    assert version.dependencies is not None and {d.target.name for d in version.dependencies} >= {"golang.org/x/mod"}
    assert [d.details for d in version.dependencies if d.target.name == "golang.org/x/sync"] == [{"indirect": True}]
    assert version.engines == {"go": "1.26.0"}
    assert not any(url.endswith(".info") for url in upstream.urls)   # the date came with `@latest`
    [artifact] = version.artifacts
    assert artifact.filename == "exp@v0.0.0-20261014143641-f0251eb4aa3e.zip"


def test_incompatible_versions_are_kept_and_the_highest_release_is_the_latest(serve) -> None:
    upstream = serve()
    package = _fetch("github.com/docker/docker")
    assert package is not None
    listed = _recorded_versions("github.com/docker/docker")
    assert sorted(v.version for v in package.versions) == sorted(listed)
    assert any(v.version.endswith("+incompatible") for v in package.versions)
    assert _version(package, "v28.5.2+incompatible").dependencies == []   # go.mod is one line: no requirements, no go
    assert _version(package, "v28.5.2+incompatible").engines is None
    # @latest names v28.5.2+incompatible; retractions are also looked for in the highest compatible tag's go.mod.
    assert [url for url in upstream.urls if url.endswith(".mod")] == [
        f"{BASE}github.com/docker/docker/@v/v28.5.2+incompatible.mod", f"{BASE}github.com/docker/docker/@v/v1.13.1.mod"]
    assert package.dist_tags == {"latest": "v28.5.2+incompatible"}
    assert _version(package, "v28.5.2+incompatible").release_date == datetime.fromisoformat(
        json.loads(_recorded("github.com/docker/docker/@v/v28.5.2+incompatible.info"))["Time"])
    assert {v.version for v in package.versions if v.dependencies is not None} == {"v28.5.2+incompatible"}


def test_the_latest_is_the_highest_release_not_a_prerelease(serve) -> None:
    upstream = serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\nv2.0.0-rc.1\nv1.9.0\n")})
    package = _fetch("example.com/m")
    assert package is not None
    assert [url for url in upstream.urls if url.endswith(".mod")] == [f"{BASE}example.com/m/@v/v1.9.0.mod"]


def test_with_prereleases_only_the_highest_prerelease_is_the_latest(serve) -> None:
    upstream = serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0-beta.1\nv1.0.0-rc.2\nv1.0.0-alpha\n")})
    assert _fetch("example.com/m") is not None
    assert [url for url in upstream.urls if url.endswith(".mod")] == [f"{BASE}example.com/m/@v/v1.0.0-rc.2.mod"]


def test_versions_the_scheme_rejects_are_skipped_with_a_log_line(serve, caplog) -> None:
    serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\nv1.2\n1.3.0\nv1.1.0\nv1.0.0\n\n")})
    with caplog.at_level(logging.WARNING, logger=goproxy.logger.name):
        package = _fetch("example.com/m")
    assert package is not None
    assert sorted(v.version for v in package.versions) == ["v1.0.0", "v1.1.0"]   # a duplicate line counts once
    assert "v1.2" in caplog.text and "1.3.0" in caplog.text
    assert package.revision == list_revision(["v1.0.0", "v1.2", "1.3.0", "v1.1.0", "v1.0.0"])


def test_the_revision_does_not_depend_on_the_order_of_the_list(serve) -> None:
    serve({**_answer(f"{BASE}example.com/a/@v/list", "v1.0.0\nv1.1.0\n"), **_answer(f"{BASE}example.com/b/@v/list", "v1.1.0\nv1.0.0\n")})
    a, b = _fetch("example.com/a"), _fetch("example.com/b")
    assert a is not None and b is not None and a.revision == b.revision
    assert a.revision == hashlib.sha256(b"v1.0.0\nv1.1.0").hexdigest()


def test_an_uppercase_path_is_escaped_in_every_url(serve) -> None:
    upstream = serve({
        **_answer(f"{BASE}github.com/!burnt!sushi/toml/@v/list", "v1.3.2\n"),
        **_answer(f"{BASE}github.com/!burnt!sushi/toml/@v/v1.3.2.info", '{"Version":"v1.3.2","Time":"2023-05-01T10:00:00Z"}'),
        **_answer(f"{BASE}github.com/!burnt!sushi/toml/@v/v1.3.2.mod", "module github.com/BurntSushi/toml\n\ngo 1.16\n"),
    })
    package = _fetch("github.com/BurntSushi/toml")
    assert package is not None
    assert package.display_name == "github.com/BurntSushi/toml"
    assert _version(package, "v1.3.2").release_date == datetime(2023, 5, 1, 10, tzinfo=UTC)
    assert _version(package, "v1.3.2").artifacts[0].download_url == f"{BASE}github.com/!burnt!sushi/toml/@v/v1.3.2.zip"
    assert _version(package, "v1.3.2").artifacts[0].filename == "toml@v1.3.2.zip"
    assert all("BurntSushi" not in url for url in upstream.urls)


def test_an_uppercase_version_is_escaped(serve) -> None:
    upstream = serve({
        **_answer(f"{BASE}example.com/m/@v/list", "v1.0.0-RC1\n"),
        **_answer(f"{BASE}example.com/m/@v/v1.0.0-!r!c1.info", '{"Version":"v1.0.0-RC1","Time":"2020-02-02T00:00:00Z"}'),
        **_answer(f"{BASE}example.com/m/@v/v1.0.0-!r!c1.mod", "module example.com/m\n"),
    })
    package = _fetch("example.com/m")
    assert package is not None and package.versions[0].release_date == datetime(2020, 2, 2, tzinfo=UTC)
    assert package.versions[0].artifacts[0].download_url == f"{BASE}example.com/m/@v/v1.0.0-!r!c1.zip"
    assert all("RC1" not in url for url in upstream.urls)


def test_a_base_url_with_a_path_and_without_a_slash(serve) -> None:
    upstream = serve({**_answer("https://corp.example/goproxy/example.com/m/@v/list", "v1.0.0\n")})
    package = _fetch("example.com/m", _adapter("https://corp.example/goproxy"))
    assert package is not None
    assert upstream.urls[0] == "https://corp.example/goproxy/example.com/m/@v/list"
    assert package.versions[0].artifacts[0].download_url == "https://corp.example/goproxy/example.com/m/@v/v1.0.0.zip"


# --- missing, errors --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_module_is_none(serve, status: int) -> None:
    upstream = serve({
        f"{BASE}example.com/m/@v/list": httpx.Response(status, text="not found"),
        f"{BASE}example.com/m/@latest": httpx.Response(status, text="not found"),
    })
    assert _fetch("example.com/m") is None
    assert upstream.urls == [f"{BASE}example.com/m/@v/list", f"{BASE}example.com/m/@latest"]


def test_a_missing_list_with_a_latest_is_a_module_with_one_version(serve) -> None:
    serve({
        f"{BASE}example.com/m/@v/list": httpx.Response(410, text="gone"),
        **_answer(f"{BASE}example.com/m/@latest", '{"Version":"v0.0.0-20200101000000-abcdef123456","Time":"2020-01-01T00:00:00Z"}'),
    })
    package = _fetch("example.com/m")
    assert package is not None and [v.version for v in package.versions] == ["v0.0.0-20200101000000-abcdef123456"]


def test_a_server_error_is_raised(serve) -> None:
    serve({f"{BASE}example.com/m/@v/list": httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        _fetch("example.com/m")


def test_a_fetch_dates_the_latest_only_and_release_date_reads_one_info(serve, caplog) -> None:
    upstream = serve({
        **_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\nv1.1.0\nv1.2.0\n"),
        **_answer(f"{BASE}example.com/m/@v/v1.0.0.info", '{"Version":"v1.0.0","Time":"2020-01-01T00:00:00Z"}'),
        f"{BASE}example.com/m/@v/v1.1.0.info": httpx.Response(500),
        f"{BASE}example.com/m/@v/v1.2.0.info": httpx.Response(200, text="not json"),
    })
    with caplog.at_level(logging.WARNING, logger=goproxy.logger.name):
        package = _fetch("example.com/m")
    assert package is not None
    # No `@latest`: the highest tag is the latest, its `.info` is unreadable, the rest waits for the background task.
    assert {v.version: v.release_date for v in package.versions} == {"v1.0.0": None, "v1.1.0": None, "v1.2.0": None}
    assert [url for url in upstream.urls if url.endswith(".info")] == [f"{BASE}example.com/m/@v/v1.2.0.info"]
    adapter, ref = _adapter(), _ref("example.com/m")
    assert asyncio.run(adapter.release_date(ref, "v1.0.0")) == datetime(2020, 1, 1, tzinfo=UTC)
    assert asyncio.run(adapter.release_date(ref, "v1.2.0")) is None
    with pytest.raises(httpx.HTTPStatusError):                          # the task marks it and tries again later
        asyncio.run(adapter.release_date(ref, "v1.1.0"))


def test_a_missing_or_unreadable_latest_go_mod_leaves_dependencies_lazy(serve, caplog) -> None:
    serve({
        **_answer(f"{BASE}example.com/a/@v/list", "v1.0.0\n"),
        **_answer(f"{BASE}example.com/b/@v/list", "v1.0.0\n"),
        **_answer(f"{BASE}example.com/b/@v/v1.0.0.mod", "module example.com/b\nrequire example.com/x\n"),
        **_answer(f"{BASE}example.com/c/@v/list", "v1.0.0\n"),
        f"{BASE}example.com/c/@v/v1.0.0.mod": httpx.Response(500),
    })
    with caplog.at_level(logging.WARNING, logger=goproxy.logger.name):
        for name in ("a", "b"):
            package = _fetch(f"example.com/{name}")
            assert package is not None and package.versions[0].dependencies is None
            assert package.versions[0].status == "active" and package.deprecation is None
        package = _fetch("example.com/c")
    assert package is not None and package.versions[0].dependencies is None
    assert "example.com/b@v1.0.0" in caplog.text and "example.com/a@v1.0.0" in caplog.text


def test_dependencies_with_invalid_module_paths_are_skipped_with_a_log_line(serve, caplog) -> None:
    mod = "module example.com/m\ngo 1.20\nrequire (\n\tfoo/bar v1.0.0\n\texample.com/ok v1.0.0\n\texample.com/x/v1 v1.0.0\n)\n"
    serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\n"), **_answer(f"{BASE}example.com/m/@v/v1.0.0.mod", mod)})
    with caplog.at_level(logging.WARNING, logger=goproxy.logger.name):
        package = _fetch("example.com/m")
    assert package is not None
    assert [d.target.name for d in package.versions[0].dependencies or []] == ["example.com/ok"]
    assert "foo/bar" in caplog.text and "example.com/x/v1" in caplog.text


def test_toolchain_goes_to_the_engines_and_the_raw_metadata(serve) -> None:
    mod = "module example.com/m\ngo 1.22.0\ntoolchain go1.22.5\n"
    serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\n"), **_answer(f"{BASE}example.com/m/@v/v1.0.0.mod", mod)})
    version = _fetch("example.com/m").versions[0]   # type: ignore[union-attr]
    assert version.engines == {"go": "1.22.0", "toolchain": "go1.22.5"}
    assert version.raw_metadata == {"go_mod": {"go": "1.22.0", "toolchain": "go1.22.5"}}
    assert version.dependencies == []


def test_transient_errors_are_retried(serve, monkeypatch) -> None:
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts < 3 else httpx.Response(200, text="v1.0.0\n")

    async def no_sleep(seconds: float) -> None:
        return None

    serve({f"{BASE}example.com/m/@v/list": flaky})
    monkeypatch.setattr(goproxy.asyncio, "sleep", no_sleep)
    adapter = _adapter()
    adapter.max_retries = 2
    assert asyncio.run(adapter.exists(_ref("example.com/m"))) is True and attempts == 3


def test_410_and_404_are_answers_not_errors(serve) -> None:
    serve({f"{BASE}example.com/gone/@v/list": httpx.Response(410), f"{BASE}example.com/gone/@latest": httpx.Response(410)})
    assert asyncio.run(_adapter().exists(_ref("example.com/gone"))) is False


# --- fetch_version_dependencies ---------------------------------------------------------------------------------


def _deps(raw: str, version: str, adapter: GoProxyAdapter | None = None):
    return asyncio.run((adapter or _adapter()).fetch_version_dependencies(_ref(raw), version, NO_LOOKUP))  # type: ignore[arg-type]


def test_fetch_version_dependencies_gives_the_go_mod_as_a_document(serve) -> None:
    serve({f"{BASE}github.com/gin-gonic/gin/@v/v1.10.0.mod": httpx.Response(
        200, text=_recorded("github.com/gin-gonic/gin/@v/v1.10.0.mod"), headers={"etag": '"abc"'})})
    result = _deps("github.com/gin-gonic/gin", "v1.10.0")
    assert result.available and result.engines == {"go": "1.20"}
    assert len(result.dependencies) == 29 and sum(1 for d in result.dependencies if d.details) == 17
    assert result.dependencies[0].raw_spec == "github.com/bytedance/sonic v1.11.6"
    [document] = result.documents
    assert document == FetchedDocument(
        path="github.com/gin-gonic/gin/@v/v1.10.0.mod", content=_recorded("github.com/gin-gonic/gin/@v/v1.10.0.mod").encode(),
        content_type="text/plain", etag='"abc"')


def test_fetch_version_dependencies_escapes_and_has_no_etag_when_the_proxy_gives_none(serve) -> None:
    upstream = serve({**_answer(f"{BASE}github.com/!burnt!sushi/toml/@v/v1.0.0-!r!c1.mod", "module github.com/BurntSushi/toml\n")})
    result = _deps("github.com/BurntSushi/toml", "v1.0.0-RC1")
    assert upstream.urls == [f"{BASE}github.com/!burnt!sushi/toml/@v/v1.0.0-!r!c1.mod"]
    assert result.dependencies == [] and result.engines is None
    assert [(d.path, d.etag) for d in result.documents] == [("github.com/!burnt!sushi/toml/@v/v1.0.0-!r!c1.mod", None)]


@pytest.mark.parametrize("status", [404, 410])
def test_fetch_version_dependencies_of_something_missing(serve, status: int) -> None:
    serve({f"{BASE}example.com/m/@v/v1.0.0.mod": httpx.Response(status)})
    result = _deps("example.com/m", "v1.0.0")
    assert result.available is False and result.dependencies == [] and result.documents == ()


def test_fetch_version_dependencies_of_an_unreadable_go_mod(serve, caplog) -> None:
    serve({**_answer(f"{BASE}example.com/m/@v/v1.0.0.mod", "module example.com/m\nrequire broken\n")})
    with caplog.at_level(logging.WARNING, logger=goproxy.logger.name):
        result = _deps("example.com/m", "v1.0.0")
    assert result.available is False and "line 2" in caplog.text


def test_fetch_version_dependencies_raises_on_a_server_error(serve) -> None:
    serve({f"{BASE}example.com/m/@v/v1.0.0.mod": httpx.Response(502)})
    with pytest.raises(httpx.HTTPStatusError):
        _deps("example.com/m", "v1.0.0")


# --- fetch_document ---------------------------------------------------------------------------------------------


def _document(path: str, adapter: GoProxyAdapter | None = None) -> FetchedDocument | None:
    return asyncio.run((adapter or _adapter()).fetch_document(path))


def test_fetch_document(serve) -> None:
    upstream = serve({f"{BASE}github.com/!burnt!sushi/toml/@v/v1.3.2.mod": httpx.Response(
        200, text="module github.com/BurntSushi/toml\n", headers={"etag": '"x"', "content-type": "text/plain; charset=UTF-8"})})
    document = _document("github.com/!burnt!sushi/toml/@v/v1.3.2.mod")
    assert document == FetchedDocument(
        path="github.com/!burnt!sushi/toml/@v/v1.3.2.mod", content=b"module github.com/BurntSushi/toml\n",
        content_type="text/plain; charset=UTF-8", etag='"x"')
    assert upstream.urls == [f"{BASE}github.com/!burnt!sushi/toml/@v/v1.3.2.mod"]


def test_fetch_document_without_etag_and_with_a_leading_slash(serve) -> None:
    serve({**_answer(f"{BASE}example.com/m/@v/v1.0.0.mod", "module example.com/m\n")})
    document = _document("/example.com/m/@v/v1.0.0.mod")
    assert document is not None and document.etag is None and document.content == b"module example.com/m\n"


@pytest.mark.parametrize("status", [404, 410])
def test_fetch_document_of_something_missing_is_none(serve, status: int) -> None:
    serve({f"{BASE}example.com/m/@v/v1.0.0.mod": httpx.Response(status, text="not found")})
    assert _document("example.com/m/@v/v1.0.0.mod") is None


def test_fetch_document_raises_on_a_server_error(serve) -> None:
    serve({f"{BASE}example.com/m/@v/v1.0.0.mod": httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        _document("example.com/m/@v/v1.0.0.mod")


# --- exists, test_connection ------------------------------------------------------------------------------------


def _exists(raw: str) -> bool:
    return asyncio.run(_adapter().exists(_ref(raw)))


def test_exists_with_versions_asks_no_latest(serve) -> None:
    upstream = serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\n")})
    assert _exists("example.com/m") is True
    assert upstream.urls == [f"{BASE}example.com/m/@v/list"]


def test_exists_by_latest_when_the_list_is_empty(serve) -> None:
    serve({
        f"{BASE}example.com/m/@v/list": httpx.Response(200, text=""),
        **_answer(f"{BASE}example.com/m/@latest", '{"Version":"v0.0.0-20200101000000-abcdef123456","Time":"2020-01-01T00:00:00Z"}'),
    })
    assert _exists("example.com/m") is True


def test_exists_is_false_when_both_say_missing(serve) -> None:
    serve({f"{BASE}example.com/m/@v/list": httpx.Response(404), f"{BASE}example.com/m/@latest": httpx.Response(410)})
    assert _exists("example.com/m") is False


def test_exists_raises_on_a_server_error(serve) -> None:
    serve({f"{BASE}example.com/m/@v/list": httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        _exists("example.com/m")


@pytest.mark.parametrize("status", [200, 404, 410])
def test_test_connection_accepts_a_reachable_proxy(serve, status: int) -> None:
    upstream = serve({f"{BASE}golang.org/x/mod/@latest": httpx.Response(status, text="{}")})
    asyncio.run(_adapter().test_connection())
    assert upstream.urls == [f"{BASE}golang.org/x/mod/@latest"]


@pytest.mark.parametrize("status", [401, 403, 500, 502])
def test_test_connection_fails_on_any_other_status(serve, status: int) -> None:
    serve({f"{BASE}golang.org/x/mod/@latest": httpx.Response(status)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())


def test_test_connection_fails_when_the_proxy_does_not_answer(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(goproxy, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(_adapter().test_connection())


def test_min_interval_is_zero() -> None:
    assert _adapter().min_interval_seconds() == 0.0


# --- the change detector ---------------------------------------------------------------------------------------


def _tracked(raw: str, revision: str | None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(ref=_ref(raw), package_id=raw, revision=revision, etag=None, unavailable=unavailable)


def test_the_detector_is_the_list_one() -> None:
    assert isinstance(_adapter().change_detector(), GoListChangeDetector)


def test_detector_reports_changed_and_missing_by_the_hash_of_the_list(serve) -> None:
    unchanged = list_revision(["v1.0.0", "v1.1.0"])
    upstream = serve({
        **_answer(f"{BASE}example.com/same/@v/list", "v1.1.0\nv1.0.0\n"),
        **_answer(f"{BASE}example.com/grown/@v/list", "v1.0.0\nv1.1.0\nv1.2.0\n"),
        f"{BASE}example.com/gone/@v/list": httpx.Response(404),
        f"{BASE}example.com/gone2/@v/list": httpx.Response(410),
        f"{BASE}example.com/already-gone/@v/list": httpx.Response(404),
        **_answer(f"{BASE}example.com/back/@v/list", "v1.0.0\nv1.1.0\n"),
        **_answer(f"{BASE}example.com/emptied/@v/list", ""),
    })
    tracked = [
        _tracked("example.com/same", unchanged), _tracked("example.com/grown", unchanged),
        _tracked("example.com/gone", unchanged), _tracked("example.com/gone2", unchanged),
        _tracked("example.com/never-synced", None), _tracked("example.com/already-gone", unchanged, unavailable=True),
        _tracked("example.com/back", unchanged, unavailable=True), _tracked("example.com/emptied", unchanged),
    ]

    result = asyncio.run(_adapter().change_detector().detect(tracked, {"anything": 1}))

    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("example.com/grown", list_revision(["v1.0.0", "v1.1.0", "v1.2.0"])),
        ("example.com/never-synced", None),
        ("example.com/back", unchanged),
    ]
    # An empty list asks `@latest`; with no answer either, the module is gone.
    assert [p.ref.name for p in result.missing] == ["example.com/gone", "example.com/gone2", "example.com/emptied"]
    assert result.cursor == {} and not result.reconcile_all
    assert all("if-none-match" not in request.headers for request in upstream.requests)   # the list has no ETag
    assert not any("never-synced" in url for url in upstream.urls)   # nothing stored: it is simply reported as changed


def test_detector_revision_is_the_one_fetch_package_stores(serve) -> None:
    serve()
    package = _fetch("github.com/redis/go-redis/v9")
    assert package is not None
    tracked = [_tracked("github.com/redis/go-redis/v9", package.revision)]
    result = asyncio.run(_adapter().change_detector().detect(tracked, {}))
    assert result.changed == [] and result.missing == []


def test_detector_bounds_the_concurrency(monkeypatch) -> None:
    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, text="v1.0.0\n")

    monkeypatch.setattr(goproxy, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    revision = list_revision(["v1.0.0"])
    tracked = [_tracked(f"example.com/m{i}", revision) for i in range(8)]
    result = asyncio.run(GoListChangeDetector(_adapter(), concurrency=3).detect(tracked, {}))
    assert result.changed == [] and result.missing == [] and peak == 3


def test_detector_survives_one_failing_package(serve, caplog) -> None:
    serve({
        **_answer(f"{BASE}example.com/ok/@v/list", "v1.0.0\nv1.1.0\n"),
        f"{BASE}example.com/broken/@v/list": httpx.Response(503),
    })
    with caplog.at_level(logging.WARNING, logger=goproxy.logger.name):
        result = asyncio.run(_adapter().change_detector().detect(
            [_tracked("example.com/ok", "old"), _tracked("example.com/broken", "old")], {}))
    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("example.com/ok", list_revision(["v1.0.0", "v1.1.0"]))]
    assert result.missing == [] and "example.com/broken" in caplog.text


def test_detector_follows_the_latest_pseudo_version_of_a_module_without_tags(serve) -> None:
    serve()
    package = _fetch("golang.org/x/exp")
    assert package is not None
    same = [_tracked("golang.org/x/exp", package.revision)]
    assert asyncio.run(_adapter().change_detector().detect(same, {})).changed == []
    older = [_tracked("golang.org/x/exp", "latest:v0.0.0-20250101000000-aaaaaaaaaaaa")]
    result = asyncio.run(_adapter().change_detector().detect(older, {}))
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("golang.org/x/exp", package.revision)]


def test_a_newest_tag_that_retracts_itself_is_seen_although_latest_names_an_older_one(serve) -> None:
    # The documented pattern: v1.0.1 retracts [v1.0.0, v1.0.1]; @latest then names v0.9.0, whose go.mod says nothing.
    serve({
        **_answer(f"{BASE}example.com/m/@v/list", "v0.9.0\nv1.0.0\nv1.0.1\n"),
        **_answer(f"{BASE}example.com/m/@latest", json.dumps({"Version": "v0.9.0", "Time": "2026-01-01T00:00:00Z"})),
        **_answer(f"{BASE}example.com/m/@v/v0.9.0.mod", "module example.com/m\n"),
        **_answer(f"{BASE}example.com/m/@v/v1.0.1.mod", "module example.com/m\n\nretract [v1.0.0, v1.0.1] // broken\n"),
    })
    package = _fetch("example.com/m")
    assert package is not None and package.dist_tags == {"latest": "v0.9.0"}
    assert {v.version: v.status for v in package.versions} == {"v0.9.0": "active", "v1.0.0": "yanked", "v1.0.1": "yanked"}


def test_a_failing_latest_does_not_lose_a_listed_module(serve) -> None:
    serve({**_answer(f"{BASE}example.com/m/@v/list", "v1.0.0\n"), f"{BASE}example.com/m/@latest": httpx.Response(503)})
    package = _fetch("example.com/m")
    assert package is not None and [v.version for v in package.versions] == ["v1.0.0"] and package.dist_tags is None
