"""The `npm-registry` adapter (WP F3) on real abbreviated packuments (tests/fixtures/npm/), recorded from
registry.npmjs.org on 2026-10-29 with `Accept: application/vnd.npm.install-v1+json`:

- left-pad: tiny, 15 versions, the latest is deprecated, only devDependencies;
- @angular/core: scoped, peerDependencies with peerDependenciesMeta, engines; trimmed to 6 of 1124 versions;
- react-dom: peerDependencies, deprecated versions; trimmed to 4 of 2950;
- @isaacs/cliui: version 8.0.2 declares `npm:` aliases (`"string-width-cjs": "npm:string-width@^4.2.0"`);
- fsevents: `os`, `bundleDependencies`; chokidar: `optionalDependencies`, also listed in `dependencies` (2.1.8).
Trimming only dropped versions (and the dist-tags pointing at them) and the `dist.signatures`; the version documents
`left-pad-1.3.0.json` and `angular-core-22.2.3.json` are `/<name>/<version>` answers with the signatures dropped."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.ecosystems.npm.changes import NpmChangesDetector

from app.ecosystems.base import NormalizedDependency, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.npm.adapters import NpmRegistryAdapter, normalize_dependencies
from app.ecosystems.npm.adapters import registry as npm_registry
from app.ecosystems.npm.adapters.registry import ACCEPT, NpmEtagChangeDetector, package_from_packument, version_from_entry
from app.ecosystems.npm.coordinates import NpmCoordinates

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "npm"
BASE = "https://registry.npmjs.org/"
LEFT_PAD_ETAG = '"e83879f942df342315ccdeff2139a89a"'
ANGULAR_ETAG = '"8b6338342d5b1f849413fc3a16cbc832"'
NO_LOOKUP = None  # the adapter does not use the lookup


def _registry(base_url: str = BASE, **config: object) -> RegistryConfig:
    return RegistryConfig(
        id="reg-npmjs", slug="npmjs", ecosystem="npm", protocol="npm-registry", base_url=base_url,
        priority=0, config=config, routing=None, auth_type="none", credentials_ref=None,
    )


def _ref(raw: str) -> PackageRef:
    return NpmCoordinates().parse(raw)


def _json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _packument(name: str, etag: str | None = None, status: int = 200) -> httpx.Response:
    headers = {"content-type": "application/vnd.npm.install-v1+json"}
    if etag:
        headers["etag"] = etag
    return httpx.Response(status, json=_json(name), headers=headers)


class Registry:
    """A fake upstream: URL -> response (or a callable), every request is kept."""

    def __init__(self, routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404, json={"error": "Not found"})
        return route(request) if callable(route) else route

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


@pytest.fixture
def serve(monkeypatch):
    def start(routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> Registry:
        registry = Registry(routes)
        monkeypatch.setattr(
            npm_registry, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(registry.handler))
        )
        return registry

    return start


def _adapter(registry: RegistryConfig | None = None) -> NpmRegistryAdapter:
    adapter = NpmRegistryAdapter(registry or _registry())
    adapter.max_retries = 0   # a 5xx in a test is an answer, not something to wait out
    return adapter


def _fetch(adapter: NpmRegistryAdapter, raw: str):
    return asyncio.run(adapter.fetch_package(_ref(raw), NO_LOOKUP))  # type: ignore[arg-type]


def _deps(adapter: NpmRegistryAdapter, raw: str, version: str):
    return asyncio.run(adapter.fetch_version_dependencies(_ref(raw), version, NO_LOOKUP))  # type: ignore[arg-type]


def _summary(dep: NormalizedDependency) -> tuple[str, str, str, str, str | None]:
    scope = f"@{dep.target.namespace}/" if dep.target.namespace else ""
    return f"{scope}{dep.target.name}", dep.constraint, dep.dependency_type, dep.raw_spec, dep.alias


# --- the plugin seams ------------------------------------------------------------------------------------------


def test_adapter_basics() -> None:
    adapter = _adapter(_registry("https://npm.corp.example/repo"))
    assert adapter.min_interval_seconds() == 0.0
    assert adapter.package_url(_ref("left-pad")) == "https://npm.corp.example/repo/left-pad"
    assert adapter.package_url(_ref("@angular/core")) == "https://npm.corp.example/repo/@angular%2fcore"
    assert adapter.version_url(_ref("@angular/core"), "1.0.0+b.1") == "https://npm.corp.example/repo/@angular%2fcore/1.0.0%2Bb.1"


# --- packuments -------------------------------------------------------------------------------------------------


def test_left_pad(serve) -> None:
    registry = serve({f"{BASE}left-pad": _packument("left-pad.json", LEFT_PAD_ETAG)})
    package = _fetch(_adapter(), "left-pad")

    assert registry.urls == [f"{BASE}left-pad"]
    assert registry.requests[0].headers["accept"] == ACCEPT
    assert package is not None
    assert (package.ref, package.registry_id, package.display_name) == (_ref("left-pad"), "reg-npmjs", "left-pad")
    assert package.revision == LEFT_PAD_ETAG
    assert package.dist_tags == {"latest": "1.3.0"} and package.deprecation is None
    assert len(package.versions) == 15 and package.versions[0].version == "0.0.3"   # the order of the packument, not sorted
    assert package.vulnerabilities is None
    # Package-level fields: the top-level copies of the latest package.json (#16).
    assert (package.description, package.homepage_url, package.repository_url) == (
        "String left pad", "https://github.com/stevemao/left-pad#readme", "https://github.com/stevemao/left-pad")
    assert (package.license_raw, package.license_spdx) == ("WTFPL", "WTFPL")
    assert package.keywords == ["leftpad", "left", "pad", "padding", "string", "repeat"]
    assert [(a.name, a.email, a.role) for a in package.authors or []] == [
        ("azer", None, "author"), ("stevemao", "maochenyan@gmail.com", "maintainer"),
        ("westlac", "cameron.westland@autodesk.com", "maintainer")]
    assert package.created_at is not None and package.updated_at is not None

    latest = package.versions[-1]
    assert latest.version == "1.3.0" and latest.status == "active"
    assert latest.release_date == datetime(2018, 4, 9, 1, 10, 45, 796000, tzinfo=UTC)   # `time` of the full packument
    assert all(v.release_date is not None for v in package.versions)
    assert latest.deprecation == {"message": "use String.prototype.padStart()"}
    assert latest.engines is None and latest.raw_metadata == {"_hasShrinkwrap": False}
    assert [_summary(d) for d in latest.dependencies or []] == [
        ("benchmark", "^2.1.0", "dev", "benchmark@^2.1.0", None),
        ("fast-check", "0.0.8", "dev", "fast-check@0.0.8", None),
        ("tape", "*", "dev", "tape@*", None),
    ]
    assert {d.scope for d in latest.dependencies or []} == {"devDependencies"}

    (artifact,) = latest.artifacts
    assert artifact.filename == "left-pad-1.3.0.tgz" and artifact.artifact_type == "tgz"
    assert artifact.download_url == "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz"
    assert artifact.checksums == {
        "sha1": "5b8a3a7765dfe001261dde915589e782f8c94d1e",
        "integrity": "sha512-XI5MPzVNApjAyhQzphX8BkmKsKUxD4LdyK24iZeQGinBN9yTQT3bFlCBy/aVx2HrNcqQGsdot8ghrjyrvMCoEA==",
    }
    assert artifact.size_bytes is None   # `unpackedSize` is not the size of the tarball

    # Dependencies are fetched lazily for every version but the latest (Q9).
    assert all(v.dependencies is None for v in package.versions[:-1])
    assert all(v.status == "active" and len(v.artifacts) == 1 for v in package.versions)


def test_checksum_keys_are_the_ones_the_pipeline_maps() -> None:
    from app.fetcher.pipeline import _CHECKSUM_COLUMNS

    (artifact,) = version_from_entry("1.3.0", _json("left-pad.json")["versions"]["1.3.0"], with_dependencies=False).artifacts
    assert set(artifact.checksums) <= set(_CHECKSUM_COLUMNS)
    assert _CHECKSUM_COLUMNS["integrity"] == "checksum_integrity"


def test_scoped_package_is_asked_for_with_an_escaped_slash(serve) -> None:
    registry = serve({f"{BASE}@angular%2fcore": _packument("angular-core.json", ANGULAR_ETAG)})
    package = _fetch(_adapter(), "@angular/core")

    assert registry.urls == [f"{BASE}@angular%2fcore"]
    assert package is not None
    assert (package.ref, package.display_name, package.revision) == (_ref("@angular/core"), "@angular/core", ANGULAR_ETAG)
    assert list(package.dist_tags or {}) == ["v17-lts", "v21-lts", "latest", "next"]
    assert [v.version for v in package.versions] == [
        "0.0.0-0", "10.0.13", "17.3.12", "21.2.25", "22.2.3", "22.3.0-next.2"]

    versions = {v.version: v for v in package.versions}
    latest = versions["22.2.3"]
    assert latest.engines == {"node": "^22.22.3 || ^24.15.0 || >=26.0.0"}
    assert latest.artifacts[0].filename == "core-22.2.3.tgz"   # the last segment of the URL, without the scope
    assert latest.artifacts[0].download_url == "https://registry.npmjs.org/@angular/core/-/core-22.2.3.tgz"
    assert [_summary(d) for d in latest.dependencies or []] == [
        ("tslib", "^2.3.0", "runtime", "tslib@^2.3.0", None),
        ("@mcp-b/webmcp-types", "^5.0.0", "dev", "@mcp-b/webmcp-types@^5.0.0", None),
        ("@mcp-b/webmcp-polyfill", "^5.0.0", "dev", "@mcp-b/webmcp-polyfill@^5.0.0", None),
        ("@modelcontextprotocol/server", "^2.0.0", "dev", "@modelcontextprotocol/server@^2.0.0", None),
        ("rxjs", "^6.5.3 || ^7.4.0", "peer", "rxjs@^6.5.3 || ^7.4.0", None),
        ("zone.js", "~0.15.0 || ~0.16.0", "peer", "zone.js@~0.15.0 || ~0.16.0", None),
        ("@angular/compiler", "22.2.3", "peer", "@angular/compiler@22.2.3", None),
    ]
    peers = {d.target.name: d for d in latest.dependencies or [] if d.dependency_type == "peer"}
    assert peers["rxjs"].details is None                                   # not in peerDependenciesMeta
    assert peers["zone.js"].details == {"optional": True}
    assert peers["compiler"].details == {"optional": True}
    assert peers["compiler"].target == PackageRef("npm", "angular", "compiler")   # scoped targets
    assert all(v.dependencies is None for v in package.versions if v is not latest)

    assert versions["10.0.13"].deprecation == {"message": "failed release"}
    assert versions["0.0.0-0"].engines is None
    assert [v.version for v in package.versions if v.deprecation] == ["10.0.13"]


def test_react_dom_peers_and_deprecations(serve) -> None:
    serve({f"{BASE}react-dom": _packument("react-dom.json", '"3d64b5e56b75fe6613fc9a28c4a9707b"')})
    package = _fetch(_adapter(), "react-dom")

    assert package is not None and package.dist_tags == {"latest": "19.3.1"}
    versions = {v.version: v for v in package.versions}
    assert list(versions) == ["0.1.0", "16.0.0", "18.2.0", "19.3.1"]
    assert [_summary(d) for d in versions["19.3.1"].dependencies or []] == [
        ("scheduler", "^0.28.0", "runtime", "scheduler@^0.28.0", None),
        ("react", "^19.3.1", "peer", "react@^19.3.1", None),
    ]
    assert versions["18.2.0"].dependencies is None
    assert versions["0.1.0"].deprecation == {"message": _json("react-dom.json")["versions"]["0.1.0"]["deprecated"]}
    assert "react-dom/server" in str((versions["16.0.0"].deprecation or {})["message"])
    assert versions["19.3.1"].deprecation is None and versions["18.2.0"].deprecation is None


def test_os_and_bundled_dependencies_are_kept_in_raw_metadata(serve) -> None:
    serve({f"{BASE}fsevents": _packument("fsevents.json")})
    package = _fetch(_adapter(), "fsevents")

    assert package is not None
    versions = {v.version: v for v in package.versions}
    assert list(versions) == ["1.1.0", "1.2.12", "1.2.13", "2.3.3"]
    assert versions["1.2.12"].raw_metadata == {"os": ["darwin"], "bundleDependencies": ["node-pre-gyp"]}
    assert versions["1.2.13"].raw_metadata == {"os": ["darwin"]}
    assert versions["1.1.0"].raw_metadata == {"bundleDependencies": ["node-pre-gyp"]}
    assert versions["1.2.12"].deprecation is not None
    assert package.revision is None   # the response carried no ETag


def test_optional_dependency_listed_in_dependencies_too_is_one_optional_entry(serve) -> None:
    serve({f"{BASE}chokidar": _packument("chokidar.json")})
    package = _fetch(_adapter(), "chokidar")
    assert package is not None
    entries = _json("chokidar.json")["versions"]

    old = entries["2.1.8"]
    assert old["dependencies"]["fsevents"] == old["optionalDependencies"]["fsevents"] == "^1.2.7"
    fsevents = [d for d in normalize_dependencies(old) if d.target.name == "fsevents"]
    assert [(d.dependency_type, d.scope, d.constraint) for d in fsevents] == [("optional", "optionalDependencies", "^1.2.7")]
    runtime = [d.target.name for d in normalize_dependencies(old) if d.dependency_type == "runtime"]
    assert "fsevents" not in runtime and "upath" in runtime

    only_optional = [d for d in normalize_dependencies(entries["3.6.0"]) if d.dependency_type == "optional"]
    assert [(d.target.name, d.constraint) for d in only_optional] == [("fsevents", "~2.3.2")]
    assert normalize_dependencies(entries["3.6.0"])[-1].dependency_type == "optional"   # after dev and peer


def test_missing_package_is_none_and_other_errors_raise(serve) -> None:
    serve({f"{BASE}broken": httpx.Response(500), f"{BASE}denied": httpx.Response(403)})
    adapter = _adapter()
    assert _fetch(adapter, "no-such-package") is None
    assert _fetch(adapter, "@no/such") is None
    for name in ("broken", "denied"):
        with pytest.raises(httpx.HTTPStatusError):
            _fetch(adapter, name)


def test_answer_that_is_not_an_object_is_an_error(serve) -> None:
    serve({f"{BASE}weird": httpx.Response(200, json=["not", "a", "packument"])})
    with pytest.raises(ValueError, match="not an object"):
        _fetch(_adapter(), "weird")


def test_package_without_versions_and_latest_not_in_versions() -> None:
    document = {"name": "x", "dist-tags": {"latest": "2.0.0", "n": 5}, "versions": {"1.0.0": {"dependencies": {"a": "1"}}, "bad": 3}}
    package = npm_registry.package_from_packument(_ref("x"), "r", document, None)
    assert package.dist_tags == {"latest": "2.0.0"}            # a non-string target is dropped
    assert [v.version for v in package.versions] == ["1.0.0"] and package.versions[0].dependencies is None
    assert npm_registry.package_from_packument(_ref("x"), "r", {}, None).versions == []


def test_display_name_is_what_the_registry_calls_the_package() -> None:
    package = npm_registry.package_from_packument(_ref("jsonstream"), "r", {"name": "JSONStream"}, None)
    assert package.display_name == "JSONStream"
    assert npm_registry.package_from_packument(_ref("@a/b"), "r", {}, None).display_name == "@a/b"


# --- one version ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "url"), [
    ({"type": "git", "url": "git+https://github.com/expressjs/express.git"}, "https://github.com/expressjs/express"),
    ({"type": "git", "url": "git+ssh://git@github.com/u/r.git", "directory": "packages/x"}, "https://github.com/u/r"),
    ("git@gitlab.com:group/sub/r.git", "https://gitlab.com/group/sub/r"),
    ("git://github.com/u/r.git", "https://github.com/u/r"),
    ("https://example.org/u/r/", "https://example.org/u/r"),
    ("github:u/r", "https://github.com/u/r"),
    ("bitbucket:u/r", "https://bitbucket.org/u/r"),
    ("u/r", "https://github.com/u/r"),
    ("", None), ("https://example.org", None), ("file:../local", None), ({"type": "git"}, None), (7, None),
])
def test_repository_url(value: object, url: str | None) -> None:
    assert npm_registry._repository_url(value) == url


@pytest.mark.parametrize(("document", "expected"), [
    ({"license": "MIT OR Apache-2.0"}, ("MIT OR Apache-2.0", "MIT OR Apache-2.0")),
    ({"license": {"type": "BSD-3-Clause", "url": "https://x"}}, ("BSD-3-Clause", "BSD-3-Clause")),
    ({"licenses": [{"type": "MIT"}, {"type": "GPL-2.0"}]}, ("MIT OR GPL-2.0", None)),
    ({"licenses": [{"type": "MIT"}]}, ("MIT", "MIT")),
    ({"license": "UNLICENSED"}, ("UNLICENSED", None)),
    ({"license": "SEE LICENSE IN LICENSE.md"}, ("SEE LICENSE IN LICENSE.md", None)),
    ({"license": " "}, (None, None)), ({"licenses": "MIT"}, (None, None)), ({}, (None, None)),
])
def test_license(document: dict, expected: tuple[str | None, str | None]) -> None:
    assert npm_registry._license(document) == expected


def test_people_in_both_forms() -> None:
    people = npm_registry._people({
        "author": "TJ Holowaychuk <tj@vision-media.ca> (http://tjholowaychuk.com)",
        "maintainers": [{"name": "wesleytodd", "email": "wes@wesleytodd.com"}, "solo", {"email": "no@name"}, 3],
    })
    assert [(p.name, p.email, p.url, p.role) for p in people] == [
        ("TJ Holowaychuk", "tj@vision-media.ca", "http://tjholowaychuk.com", "author"),
        ("wesleytodd", "wes@wesleytodd.com", None, "maintainer"),
        ("solo", None, None, "maintainer"),
    ]
    assert npm_registry._people({}) == []


def test_package_fields_that_are_not_what_they_should_be_are_left_out() -> None:
    document = {"description": 5, "homepage": "", "keywords": "a,b", "time": {"created": "never"}}
    package = npm_registry.package_from_packument(_ref("x"), "r", document, None)
    assert (package.description, package.homepage_url, package.keywords, package.created_at) == (None, None, None, None)


def test_version_from_entry_details() -> None:
    entry = {
        "deprecated": "", "engines": ["node >=0.8"], "os": [], "bundledDependencies": ["a"], "_hasShrinkwrap": True,
        "cpu": ["x64"], "dist": {"tarball": "https://r.example/x/-/x%2B1-1.0.0.tgz", "shasum": "ABCDEF", "unpackedSize": 12},
    }
    version = version_from_entry("1.0.0", entry, with_dependencies=True)
    assert version.deprecation is None                   # an empty message un-deprecates
    assert version.engines == {"cpu": "x64"}             # old packages publish `engines` as a list; cpu joins them
    assert version.raw_metadata == {"cpu": ["x64"], "bundleDependencies": ["a"], "_hasShrinkwrap": True}
    assert version.dependencies == []                    # none declared, and loaded
    (artifact,) = version.artifacts
    assert artifact.filename == "x+1-1.0.0.tgz"          # percent-decoded
    assert artifact.checksums == {"sha1": "abcdef"}      # no integrity published

    assert version_from_entry("1.0.0", {"dist": {"shasum": "x"}}, with_dependencies=False).artifacts == []
    assert version_from_entry("1.0.0", {"bundleDependencies": True}, with_dependencies=False).raw_metadata == {"bundleDependencies": True}
    assert version_from_entry("1.0.0", {"bundleDependencies": []}, with_dependencies=False).raw_metadata == {}
    assert version_from_entry("1.0.0", {"_hasShrinkwrap": False}, with_dependencies=False).raw_metadata == {"_hasShrinkwrap": False}
    assert version_from_entry("1.0.0", {"engines": {"node": ">=18", "npm": 9}}, with_dependencies=False).engines == {"node": ">=18"}


def test_release_dates_come_from_time() -> None:
    document = {"versions": {"1.0.0": {}, "2.0.0": {}, "3.0.0": {}},
                "time": {"created": "2020-01-01T00:00:00.000Z", "modified": "2021-01-01T00:00:00.000Z",
                         "1.0.0": "2020-01-01T00:00:00.000Z", "2.0.0": "not a date"}}
    package = package_from_packument(_ref("a"), "r", document, None)
    assert [v.release_date for v in package.versions] == [datetime(2020, 1, 1, tzinfo=UTC), None, None]
    # The abbreviated packument has no `time`: no dates, nothing fails.
    assert package_from_packument(_ref("a"), "r", {"versions": {"1.0.0": {}}}, None).versions[0].release_date is None


def test_release_date_reaches_the_version() -> None:
    date = datetime(2024, 1, 2, tzinfo=UTC)
    assert version_from_entry("1.0.0", {}, with_dependencies=False, release_date=date).release_date == date
    assert version_from_entry("1.0.0", {}, with_dependencies=False).release_date is None


# --- dependency normalization -----------------------------------------------------------------------------------


def test_alias_from_a_real_package() -> None:
    entries = _json("isaacs-cliui.json")["versions"]
    deps = {d.raw_spec.split("@", 1)[0]: d for d in normalize_dependencies(entries["8.0.2"])}

    cjs = deps["string-width-cjs"]
    assert (cjs.target, cjs.constraint, cjs.alias, cjs.dependency_type) == (
        PackageRef("npm", "", "string-width"), "^4.2.0", "string-width-cjs", "runtime")
    assert cjs.raw_spec == "string-width-cjs@npm:string-width@^4.2.0" and cjs.details is None
    plain = deps["string-width"]
    assert (plain.target.name, plain.constraint, plain.alias) == ("string-width", "^5.1.2", None)
    # The same package under two names are two edges with different constraints.
    runtime = [d for d in normalize_dependencies(entries["8.0.2"]) if d.dependency_type == "runtime"]
    assert sorted(d.target.name for d in runtime) == ["string-width", "string-width", "strip-ansi", "strip-ansi", "wrap-ansi", "wrap-ansi"]
    assert sorted(d.alias for d in runtime if d.alias) == ["string-width-cjs", "strip-ansi-cjs", "wrap-ansi-cjs"]


def test_alias_forms() -> None:
    def one(name: str, spec: str, field: str = "dependencies") -> NormalizedDependency:
        (dep,) = normalize_dependencies({field: {name: spec}})
        return dep

    scoped = one("foo", "npm:@scope/bar@^1.2.3")
    assert (scoped.target, scoped.constraint, scoped.alias) == (PackageRef("npm", "scope", "bar"), "^1.2.3", "foo")
    latest = one("foo", "npm:bar")
    assert (latest.target.name, latest.constraint, latest.alias) == ("bar", "", "foo")
    scoped_latest = one("foo", "npm:@scope/bar")
    assert (scoped_latest.target, scoped_latest.constraint) == (PackageRef("npm", "scope", "bar"), "")
    to_scoped = one("@me/foo", "npm:bar@1")
    assert (to_scoped.target.name, to_scoped.alias, to_scoped.raw_spec) == ("bar", "@me/foo", "@me/foo@npm:bar@1")
    assert one("bar", "npm:bar@^1").alias is None                     # the same name is no alias
    assert one("foo", "npm:bar@^1", "devDependencies").dependency_type == "dev"
    broken = one("foo", "npm:")
    assert (broken.target.name, broken.constraint, broken.details) == ("foo", "", {"unresolvable": "npm:"})


@pytest.mark.parametrize("spec", [
    "git+https://github.com/user/repo.git#v1.0.0",
    "git+ssh://git@github.com/user/repo.git",
    "git://github.com/user/repo.git",
    "git@github.com:user/repo.git",
    "github:user/repo#semver:^1",
    "user/repo",
    "user/repo#v2",
    "gitlab:user/repo",
    "bitbucket:user/repo",
    "gist:11081aaa281",
    "file:../local-pkg",
    "file:./vendor/pkg.tgz",
    "link:../sibling",
    "http://example.com/pkg.tgz",
    "https://example.com/pkg-1.0.0.tgz",
    "workspace:*",
    "workspace:^1.0.0",
    "./relative",
    "../up",
    "/abs/path",
    "~/home/pkg",
])
def test_non_registry_specs_keep_the_name_and_are_marked_unresolvable(spec: str) -> None:
    (dep,) = normalize_dependencies({"devDependencies": {"@corp/tool": spec}})
    assert dep.target == PackageRef("npm", "corp", "tool")
    assert dep.constraint == "" and dep.alias is None
    assert dep.details == {"unresolvable": spec}
    assert (dep.dependency_type, dep.scope, dep.raw_spec) == ("dev", "devDependencies", f"@corp/tool@{spec}")


@pytest.mark.parametrize("spec", [
    "^1.2.3", "~1.2", "1.x", "*", "", "latest", "next", ">=1.0.0 <2.0.0 || ^3", "1.0.0 - 2.0.0", "1.2.3-beta.1+build.5",
])
def test_registry_specs_are_constraints_as_written(spec: str) -> None:
    (dep,) = normalize_dependencies({"dependencies": {"foo": spec}})
    assert (dep.constraint, dep.details, dep.alias) == (spec, None, None)
    assert dep.raw_spec == f"foo@{spec}"


def test_dependency_types_scopes_and_order() -> None:
    manifest = {
        "optionalDependencies": {"opt": "1", "both": "2"},
        "peerDependencies": {"peer": "3", "peer-opt": "4", "meta-false": "5", "meta-junk": "6"},
        "devDependencies": {"dev": "7", "both": "8"},
        "dependencies": {"run": "9", "both": "10", "opt": "1"},
        "peerDependenciesMeta": {
            "peer-opt": {"optional": True}, "meta-false": {"optional": False}, "meta-junk": "x", "unlisted": {"optional": True}},
    }
    deps = normalize_dependencies(manifest)
    assert [(d.target.name, d.dependency_type, d.scope) for d in deps] == [
        ("run", "runtime", "dependencies"),                      # `both` and `opt` are optional: not repeated here
        ("dev", "dev", "devDependencies"),
        ("both", "dev", "devDependencies"),                      # a dev entry is a separate edge
        ("peer", "peer", "peerDependencies"),
        ("peer-opt", "peer", "peerDependencies"),
        ("meta-false", "peer", "peerDependencies"),
        ("meta-junk", "peer", "peerDependencies"),
        ("opt", "optional", "optionalDependencies"),
        ("both", "optional", "optionalDependencies"),
    ]
    assert {d.target.name: d.details for d in deps if d.dependency_type == "peer"} == {
        "peer": None, "peer-opt": {"optional": True}, "meta-false": None, "meta-junk": None}
    assert next(d for d in deps if d.dependency_type == "optional" and d.target.name == "both").constraint == "2"


def test_optional_peer_that_is_also_unresolvable_keeps_both_marks() -> None:
    manifest = {"peerDependencies": {"x": "github:a/b"}, "peerDependenciesMeta": {"x": {"optional": True}}}
    (dep,) = normalize_dependencies(manifest)
    assert dep.details == {"optional": True, "unresolvable": "github:a/b"}


def test_garbage_in_a_manifest_does_not_fail_the_package() -> None:
    manifest = {
        "dependencies": {"ok": "^1", "not-a-spec": {"version": "1"}, "none": None, "bad/name": "1", "@scope": "1", "": "1"},
        "devDependencies": ["not", "a", "mapping"],
        "peerDependencies": "nope",
        "optionalDependencies": None,
    }
    assert [d.target.name for d in normalize_dependencies(manifest)] == ["ok"]
    assert normalize_dependencies({}) == []


def test_legacy_names_with_capitals_are_dependency_targets() -> None:
    (dep,) = normalize_dependencies({"dependencies": {"JSONStream": "^1.3.5"}})
    assert dep.target == PackageRef("npm", "", "JSONStream") and dep.raw_spec == "JSONStream@^1.3.5"


# --- the dependencies of one version ------------------------------------------------------------------------------


def test_version_dependencies_from_the_version_document(serve) -> None:
    registry = serve({
        f"{BASE}left-pad/1.3.0": httpx.Response(200, json=_json("left-pad-1.3.0.json")),
        f"{BASE}@angular%2fcore/22.2.3": httpx.Response(200, json=_json("angular-core-22.2.3.json")),
    })
    adapter = _adapter()

    result = _deps(adapter, "left-pad", "1.3.0")
    assert result.available and result.engines is None and result.documents == ()
    # The version document is sorted by key, the packument entry is not: compare without the order.
    assert sorted(_summary(d) for d in result.dependencies) == [
        ("benchmark", "^2.1.0", "dev", "benchmark@^2.1.0", None),
        ("fast-check", "0.0.8", "dev", "fast-check@0.0.8", None),
        ("tape", "*", "dev", "tape@*", None),
    ]

    result = _deps(adapter, "@angular/core", "22.2.3")
    assert result.engines == {"node": "^22.22.3 || ^24.15.0 || >=26.0.0"}
    assert [d.dependency_type for d in result.dependencies].count("peer") == 3
    assert {d.target.name for d in result.dependencies if d.details == {"optional": True}} == {"zone.js", "compiler"}

    assert registry.urls == [f"{BASE}left-pad/1.3.0", f"{BASE}@angular%2fcore/22.2.3"]
    assert registry.requests[0].headers["accept"] == "application/json"


def test_the_version_document_agrees_with_the_packument_entry() -> None:
    entry = _json("angular-core.json")["versions"]["22.2.3"]
    document = _json("angular-core-22.2.3.json")

    def by_edge(deps: list[NormalizedDependency]) -> list[NormalizedDependency]:
        return sorted(deps, key=lambda d: (d.dependency_type, d.raw_spec))   # the document is sorted by key

    assert by_edge(normalize_dependencies(entry)) == by_edge(normalize_dependencies(document))
    packument_version = version_from_entry("22.2.3", entry, with_dependencies=True)
    assert by_edge(packument_version.dependencies or []) == by_edge(normalize_dependencies(document))
    assert packument_version.engines == document["engines"]


def test_missing_version_is_unavailable_not_an_error(serve) -> None:
    serve({})
    result = _deps(_adapter(), "left-pad", "9.9.9")
    assert result.available is False and result.dependencies == [] and result.engines is None


def test_version_with_build_metadata_is_escaped_in_the_url(serve) -> None:
    registry = serve({f"{BASE}demo/1.0.0%2Bbuild.5": httpx.Response(200, json={"dependencies": {"a": "^1"}})})
    result = _deps(_adapter(), "demo", "1.0.0+build.5")
    assert registry.urls == [f"{BASE}demo/1.0.0%2Bbuild.5"] and [d.target.name for d in result.dependencies] == ["a"]


def test_version_dependencies_errors_other_than_404_raise(serve) -> None:
    serve({f"{BASE}demo/1.0.0": httpx.Response(502)})
    with pytest.raises(httpx.HTTPStatusError):
        _deps(_adapter(), "demo", "1.0.0")


# --- exists, connection, retries ----------------------------------------------------------------------------------


def test_exists_is_true_for_a_packument_false_for_404_and_other_errors_raise(serve) -> None:
    serve({f"{BASE}left-pad": _packument("left-pad.json"), f"{BASE}broken": httpx.Response(500)})
    adapter = _adapter()
    assert asyncio.run(adapter.exists(_ref("left-pad"))) is True
    assert asyncio.run(adapter.exists(_ref("@no/such"))) is False
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.exists(_ref("broken")))


def test_connection_check_reads_the_ping(serve) -> None:
    registry = serve({f"{BASE}-/ping": httpx.Response(200, json={})})
    asyncio.run(_adapter().test_connection())
    assert registry.urls == [f"{BASE}-/ping"]

    registry = serve({"https://npm.corp.example/repo/-/ping": httpx.Response(200, text="")})   # GitHub Packages: empty body
    asyncio.run(_adapter(_registry("https://npm.corp.example/repo")).test_connection())
    assert registry.urls == ["https://npm.corp.example/repo/-/ping"]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_connection_check_fails_for_a_wrong_url_or_an_unauthorized_source(serve, status: int) -> None:
    serve({f"{BASE}-/ping": httpx.Response(status)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())


def test_transient_errors_are_retried(serve, monkeypatch) -> None:
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else _packument("left-pad.json")

    serve({f"{BASE}left-pad": flaky})

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(npm_registry.asyncio, "sleep", no_sleep)
    adapter = NpmRegistryAdapter(_registry())
    adapter.max_retries = 2
    assert _fetch(adapter, "left-pad") is not None and len(calls) == 2


# --- change detection ------------------------------------------------------------------------------------------


def _tracked(raw: str, revision: str | None, etag: str | None = None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(ref=_ref(raw), package_id=raw, revision=revision, etag=etag, unavailable=unavailable)


def _conditional(etag: str, new_etag: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == etag and new_etag is None:
            return httpx.Response(304, headers={"etag": etag})
        return _packument("left-pad.json", etag=new_etag or etag)

    return answer


def test_detector_is_the_etag_one_unless_a_changes_feed_is_configured() -> None:
    assert isinstance(_adapter().change_detector(), NpmEtagChangeDetector)
    assert isinstance(_adapter(_registry(changes_url="")).change_detector(), NpmEtagChangeDetector)
    feed = _adapter(_registry(changes_url="https://replicate.npmjs.com/registry/")).change_detector()
    assert isinstance(feed, NpmChangesDetector) and feed.changes_url == "https://replicate.npmjs.com/registry/"


def test_detector_asks_conditionally_and_reports_changed_and_missing(serve) -> None:
    registry = serve({
        f"{BASE}same": _conditional('"a"'),
        f"{BASE}changed": _conditional('"old"', new_etag='"new"'),
        f"{BASE}gone": httpx.Response(404),
        f"{BASE}back": _conditional('"b"'),
        f"{BASE}still-gone": httpx.Response(404),
        f"{BASE}from-etag-column": _conditional('"a"'),
        f"{BASE}@corp%2fui": _conditional('"c"'),
    })
    tracked = [
        _tracked("same", '"a"'), _tracked("changed", '"old"'), _tracked("gone", '"g"'), _tracked("never-synced", None),
        _tracked("back", '"b"', unavailable=True), _tracked("still-gone", '"s"', unavailable=True),
        _tracked("from-etag-column", None, etag='"a"'), _tracked("@corp/ui", '"c"'),
    ]

    result = asyncio.run(_adapter().change_detector().detect(tracked, {"anything": 1}))

    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("changed", '"new"'), ("never-synced", None), ("back", '"b"')]
    assert [p.ref.name for p in result.missing] == ["gone"]
    assert result.cursor == {} and not result.reconcile_all
    sent = {str(r.url).removeprefix(BASE): (r.headers.get("if-none-match"), r.headers["accept"]) for r in registry.requests}
    assert sent["same"] == ('"a"', ACCEPT) and sent["changed"] == ('"old"', ACCEPT)   # the ETag belongs to the representation
    assert sent["from-etag-column"][0] == '"a"' and sent["@corp%2fui"][0] == '"c"'
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

    monkeypatch.setattr(npm_registry, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    result = asyncio.run(NpmEtagChangeDetector(_adapter(), concurrency=3).detect([_tracked(f"p{i}", '"a"') for i in range(8)], {}))
    assert result.changed == [] and result.missing == [] and peak == 3


def test_detector_survives_one_failing_package(serve) -> None:
    serve({f"{BASE}changed": _conditional('"old"', new_etag='"new"'), f"{BASE}broken": httpx.Response(503)})
    result = asyncio.run(_adapter().change_detector().detect([_tracked("changed", '"old"'), _tracked("broken", '"b"')], {}))
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("changed", '"new"')]
    assert result.missing == []



def test_os_and_cpu_become_engines_in_npm_syntax() -> None:
    entry = {"os": ["darwin", "!win32"], "cpu": ["arm64", "x64"], "engines": {"node": ">=18"}}
    assert version_from_entry("1.0.0", entry, with_dependencies=False).engines == {
        "node": ">=18", "os": "darwin,!win32", "cpu": "arm64,x64"}
