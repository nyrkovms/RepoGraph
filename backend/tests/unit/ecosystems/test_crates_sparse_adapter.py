"""The `cargo-sparse` adapter (WP G3) on real index files and API answers (tests/fixtures/crates/), recorded on
2026-10-30 with `curl -A "PackageHub/0.1 (+https://github.com/nyrkovms/RepoGraph)"`:

- serde (index.crates.io/se/rd/serde): three yanked versions, `rust_version` on recent ones, `serde_derive` optional;
  trimmed to 13 of 316;
- tokio (to/ki/tokio): `features2` with `dep:` (1.48.0 and later), cfg targets, the same crate several times with
  different targets; trimmed to 12 of 201 (the file is in publication order, not version order);
- reqwest (re/qw/reqwest): `package =` renames (`native-tls-crate`, `cookie_crate`), `x?/feat` values; trimmed to 9 of 126;
- link-cplusplus (li/nk/link-cplusplus): `links`, a build dependency; all 13 versions;
- api-serde.json (crates.io/api/v1/crates/serde): `versions` trimmed to 1.0.229, 1.0.228 (`MIT OR Apache-2.0`) and
  1.0.91, 1.0.90 (the legacy `MIT/Apache-2.0`), the rest as recorded;
- api-serde-json-include.json (crates.io/api/v1/crates/serde-json?include=): as recorded.
Trimming only dropped whole index lines (versions) and API versions; the ETags below are those of the full files.
Everything is served by a fake upstream; nothing here reaches the network."""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.ecosystems.base import NormalizedDependency, NormalizedPackage, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.crates.adapters import CratesSparseAdapter, enabled_by, normalize_dependencies
from app.ecosystems.crates.adapters import sparse
from app.ecosystems.crates.adapters.sparse import (
    CratesEtagChangeDetector,
    apply_api_document,
    download_url,
    merged_features,
)
from app.ecosystems.crates.coordinates import CratesCoordinates

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "crates"
BASE = "https://index.crates.io/"
API = "https://crates.io"
SERDE_ETAG = '"971e1a71d7d6afbfc90a23ad91931103"'
TOKIO_ETAG = '"4af00b7cf94190b10be753840f34481f"'
REQWEST_ETAG = '"47ef5efc76632e7a3720b53d6e0633a7"'
NO_LOOKUP = None  # the adapter does not use the lookup
CONFIG = {"dl": "https://static.crates.io/crates", "api": "https://crates.io"}


def _registry(base_url: str = BASE, **config: object) -> RegistryConfig:
    return RegistryConfig(
        id="reg-crates", slug="crates-io", ecosystem="crates", protocol="cargo-sparse", base_url=base_url,
        priority=0, config=config, routing=None, auth_type="none", credentials_ref=None,
    )


def _ref(raw: str) -> PackageRef:
    return CratesCoordinates().parse(raw)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _index(name: str, etag: str | None = None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=_text(f"{name}.jsonl"), headers={"etag": etag} if etag else {})


def _api(name: str) -> httpx.Response:
    return httpx.Response(200, json=json.loads(_text(name)))


def _lines(name: str) -> list[dict]:
    return [json.loads(line) for line in _text(f"{name}.jsonl").splitlines()]


class Upstream:
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
    def start(routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> Upstream:
        upstream = Upstream({f"{BASE}config.json": httpx.Response(200, json=CONFIG), **routes})
        monkeypatch.setattr(
            sparse, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
        )
        return upstream

    return start


@pytest.fixture(autouse=True)
def turns(monkeypatch) -> list[tuple[str, float]]:
    """The API limiter is Redis; here it only records who asked for a turn."""
    asked: list[tuple[str, float]] = []

    async def wait_turn(key: str, interval_s: float) -> None:
        asked.append((key, interval_s))

    monkeypatch.setattr(sparse, "wait_turn", wait_turn)
    return asked


def _adapter(registry: RegistryConfig | None = None, **config: object) -> CratesSparseAdapter:
    adapter = CratesSparseAdapter(registry or _registry(api_url=API, **config))
    adapter.max_retries = 0   # a 5xx in a test is an answer, not something to wait out
    return adapter


def _fetch(adapter: CratesSparseAdapter, raw: str) -> NormalizedPackage | None:
    return asyncio.run(adapter.fetch_package(_ref(raw), NO_LOOKUP))  # type: ignore[arg-type]


def _deps(adapter: CratesSparseAdapter, raw: str, version: str):
    return asyncio.run(adapter.fetch_version_dependencies(_ref(raw), version, NO_LOOKUP))  # type: ignore[arg-type]


def _version(package: NormalizedPackage, number: str):
    return next(v for v in package.versions if v.version == number)


def _dep(version, local: str, *, kind: str | None = None, nth: int = 0) -> NormalizedDependency:
    """The dependency by its raw_spec's local name (a crate may list one several times, per target)."""
    found = [d for d in version.dependencies if d.raw_spec.split(" =")[0] == local and (kind is None or d.scope == kind)]
    return found[nth]


SERDE_URLS = [f"{BASE}se/rd/serde", f"{BASE}config.json", f"{API}/api/v1/crates/serde"]


# --- the plugin seams ------------------------------------------------------------------------------------------


def test_adapter_basics() -> None:
    adapter = _adapter(_registry("https://registry.corp.example/index", api_url="https://crates.corp.example/"))
    assert adapter.min_interval_seconds() == 0.0
    assert adapter.index_url(_ref("serde")) == "https://registry.corp.example/index/se/rd/serde"
    assert adapter.index_url(_ref("Inflector")) == "https://registry.corp.example/index/in/fl/inflector"   # lowercase path
    assert adapter.index_url(_ref("cc")) == "https://registry.corp.example/index/2/cc"
    assert adapter.index_url(_ref("syn")) == "https://registry.corp.example/index/3/s/syn"
    assert adapter.index_url(_ref("a")) == "https://registry.corp.example/index/1/a"
    assert adapter.api_crate_url("serde") == "https://crates.corp.example/api/v1/crates/serde"
    assert _adapter(_registry()).api_crate_url("serde") is None


# --- serde -----------------------------------------------------------------------------------------------------


def test_serde(serve, turns) -> None:
    upstream = serve({f"{BASE}se/rd/serde": _index("serde", SERDE_ETAG), f"{API}/api/v1/crates/serde": _api("api-serde.json")})
    package = _fetch(_adapter(), "serde")

    assert sorted(upstream.urls) == sorted(SERDE_URLS)
    assert package is not None
    assert (package.ref, package.registry_id, package.display_name) == (_ref("serde"), "reg-crates", "serde")
    assert package.revision == SERDE_ETAG
    assert [v.version for v in package.versions] == [
        "0.0.0", "0.2.0", "0.7.6", "1.0.31", "1.0.90", "1.0.91", "1.0.95",
        "1.0.224", "1.0.225", "1.0.226", "1.0.227", "1.0.228", "1.0.229"]   # the order of the file
    assert {v.version for v in package.versions if v.status == "yanked"} == {"0.7.6", "1.0.31", "1.0.95"}
    assert package.dist_tags is None and package.vulnerabilities is None and package.authors is None

    old, newest = package.versions[0], package.versions[-1]
    assert old.release_date == datetime(2014, 12, 5, 20, 20, 39, tzinfo=UTC)
    assert newest.release_date == datetime(2026, 7, 18, 23, 5, 13, tzinfo=UTC)
    assert old.engines is None and newest.engines == {"rust": "1.56"}
    assert newest.features == {
        "alloc": ["serde_core/alloc"], "default": ["std"], "derive": ["serde_derive"], "rc": ["serde_core/rc"],
        "std": ["serde_core/std"], "unstable": ["serde_core/unstable"],
    }
    assert newest.raw_metadata == {} and newest.deprecation is None
    [artifact] = newest.artifacts
    assert (artifact.filename, artifact.artifact_type) == ("serde-1.0.229.crate", "crate")
    assert artifact.download_url == "https://static.crates.io/crates/serde/1.0.229/download"
    assert artifact.checksums == {"sha256": "4148590afebada386688f18773da617792bf2ef03ffc1e4cbd2b1d45b023e0ba"}

    # The API: description, links, keywords, counters, license of the default version.
    assert (package.description, package.homepage_url) == ("A generic serialization/deserialization framework", "https://serde.rs")
    assert (package.repository_url, package.documentation_url) == ("https://github.com/serde-rs/serde", "https://docs.rs/serde")
    assert package.keywords == ["no_std", "serde", "serialization"]
    assert (package.downloads_total, package.downloads_recent) == (1615476642, 393383662)
    assert package.created_at == datetime(2014, 12, 5, 20, 20, 39, 487502, tzinfo=UTC)
    assert package.updated_at == datetime(2026, 7, 18, 23, 5, 13, 266456, tzinfo=UTC)
    assert (package.license_raw, package.license_spdx) == ("MIT OR Apache-2.0", "MIT OR Apache-2.0")
    assert turns == [("crates-io:api", 1.0)]


def test_serde_dependencies(serve) -> None:
    serve({f"{BASE}se/rd/serde": _index("serde"), f"{API}/api/v1/crates/serde": _api("api-serde.json")})
    package = _fetch(_adapter(), "serde")
    assert package is not None

    core, derive = _version(package, "1.0.229").dependencies
    assert (core.target, core.constraint, core.dependency_type, core.scope) == (_ref("serde_core"), "=1.0.229", "runtime", "normal")
    assert core.raw_spec == 'serde_core = "=1.0.229"' and core.alias is None and core.extra_name is None
    assert core.environment_markers is None
    assert core.details == {"features": ["result"], "default_features": False}
    # `serde_derive` is named by no `dep:`, so it is an implicit feature of its own next to `derive`.
    assert (derive.dependency_type, derive.scope, derive.constraint) == ("optional", "normal", "^1")
    assert derive.extra_name == "serde_derive" and derive.details == {"enabled_by": ["derive", "serde_derive"]}
    # Dependencies are filled in for every version, not only the newest.
    assert all(v.dependencies is not None for v in package.versions)
    assert _version(package, "0.0.0").dependencies == []


def test_the_license_with_a_slash_is_an_or(serve) -> None:
    document = json.loads(_text("api-serde.json"))
    document["crate"]["default_version"] = "1.0.91"
    serve({f"{BASE}se/rd/serde": _index("serde"), f"{API}/api/v1/crates/serde": httpx.Response(200, json=document)})
    package = _fetch(_adapter(), "serde")
    assert package is not None
    assert (package.license_raw, package.license_spdx) == ("MIT/Apache-2.0", "MIT OR Apache-2.0")


def _package() -> NormalizedPackage:
    return NormalizedPackage(ref=_ref("serde"), registry_id="r", display_name="serde", revision=None, versions=[])


def test_license_comes_from_the_default_version_then_the_latest_stable_then_the_newest_listed() -> None:
    versions = [
        {"num": "1.0.1", "license": "MIT", "created_at": "2020-01-02T00:00:00Z"},
        {"num": "1.0.2", "license": "Apache-2.0", "created_at": "2020-01-03T00:00:00Z"},
        {"num": "1.0.0", "license": "ISC", "created_at": "2020-01-01T00:00:00Z"},
    ]

    def license_of(**crate: object) -> tuple[str | None, str | None]:
        package = _package()
        apply_api_document(package, {"crate": {"name": "x", **crate}, "versions": versions})
        return package.license_raw, package.license_spdx

    assert license_of(default_version="1.0.0", max_stable_version="1.0.1") == ("ISC", "ISC")
    assert license_of(default_version=None, max_stable_version="1.0.1") == ("MIT", "MIT")
    assert license_of(default_version="9.9.9", max_stable_version="1.0.1") == ("MIT", "MIT")   # not listed: next in line
    assert license_of() == ("Apache-2.0", "Apache-2.0")   # the newest by date, whatever the order of the list
    package = _package()
    apply_api_document(package, {"crate": {"name": "x", "default_version": "1.0.0"}, "versions": [{"num": "1.0.0"}]})
    assert (package.license_raw, package.license_spdx) == (None, None)
    apply_api_document(package, {"crate": {"name": "x"}})
    assert (package.license_raw, package.license_spdx, package.keywords) == (None, None, None)


# --- tokio -----------------------------------------------------------------------------------------------------


def _tokio(serve):
    serve({f"{BASE}to/ki/tokio": _index("tokio", TOKIO_ETAG)})
    package = _fetch(_adapter(_registry()), "tokio")
    assert package is not None
    return package


def test_tokio_keeps_the_file_order_and_yanked(serve) -> None:
    package = _tokio(serve)
    assert package.revision == TOKIO_ETAG
    assert [v.version for v in package.versions] == [
        "0.0.0", "0.1.0", "0.1.12", "1.0.0", "1.39.0", "1.48.0", "1.51.5", "1.53.2", "1.51.6", "1.53.3", "1.54.0", "1.54.1"]
    assert {v.version for v in package.versions if v.status == "yanked"} == {"0.1.12", "1.0.0", "1.39.0", "1.54.0"}
    assert _version(package, "1.54.1").engines == {"rust": "1.85"}
    # No API configured: the package-level fields stay empty, the index alone says nothing about them.
    assert (package.description, package.license_raw, package.keywords, package.downloads_total) == (None, None, None, None)


def test_tokio_features2_is_merged_into_features(serve) -> None:
    features = _version(_tokio(serve), "1.54.1").features
    assert features is not None
    assert features["io-uring"] == ["dep:io-uring", "libc", "mio/os-poll", "mio/os-ext", "dep:slab"]   # from features2
    assert features["taskdump"] == ["dep:backtrace"]
    assert features["full"][:3] == ["fs", "io-util", "io-std"] and features["default"] == []   # from features
    old = _lines("tokio")[0]
    assert "features2" not in old and merged_features(old) == old["features"]


def test_tokio_cfg_targets_become_markers(serve) -> None:
    version = _version(_tokio(serve), "1.54.1")
    backtrace = _dep(version, "backtrace", kind="normal")
    assert backtrace.environment_markers == {"rust_cfg": 'cfg(all(tokio_unstable, target_os = "linux"))'}
    assert _dep(version, "bytes").environment_markers is None
    assert [d.environment_markers["rust_cfg"] for d in version.dependencies if d.raw_spec.startswith("libc =")] == [
        'cfg(all(tokio_unstable, target_os = "linux"))', 'cfg(target_os = "wasi")', "cfg(unix)", "cfg(unix)"]
    # The kind decides the scope and the type; an optional dependency is `optional` whatever its kind column says.
    assert [(d.scope, d.dependency_type) for d in version.dependencies if d.raw_spec.startswith("libc =")] == [
        ("normal", "optional")] * 3 + [("dev", "dev")]
    assert _dep(version, "mockall").dependency_type == "dev"


def test_tokio_optional_dependencies_know_what_enables_them(serve) -> None:
    version = _version(_tokio(serve), "1.54.1")
    # `bytes`: named by `io-util` and `process`, and `full` includes both; it is also its own implicit feature.
    bytes_ = _dep(version, "bytes")
    assert bytes_.details == {"enabled_by": ["bytes", "full", "io-util", "process"]} and bytes_.extra_name == "bytes"
    # `dep:` in features2 hides the implicit feature: no `backtrace` feature, and so no extra_name.
    backtrace = _dep(version, "backtrace", kind="normal")
    assert backtrace.details == {"enabled_by": ["taskdump"]} and backtrace.extra_name is None
    slab = _dep(version, "slab")
    assert slab.details == {"enabled_by": ["io-uring"]} and slab.extra_name is None
    # `mio/os-poll` turns on the optional dependency `mio`; `libc` is a bare name in an old-syntax feature list.
    assert _dep(version, "mio", nth=0).details == {
        "default_features": False, "enabled_by": ["full", "io-uring", "mio", "net", "process", "signal"]}
    assert _dep(version, "libc", nth=0).details == {"enabled_by": ["full", "io-uring", "libc", "net", "process", "signal"]}
    assert _dep(version, "tokio-macros").details == {"enabled_by": ["full", "macros", "tokio-macros"]}
    assert _dep(version, "tokio-macros").constraint == "~2.7.0"
    # Not optional: nothing to enable.
    pin = _dep(version, "pin-project-lite")
    assert (pin.dependency_type, pin.details, pin.extra_name) == ("runtime", None, None)


def test_tokio_dependency_details(serve) -> None:
    version = _version(_tokio(serve), "1.54.1")
    assert _dep(version, "futures").details == {"features": ["async-await"]}
    assert _dep(version, "nix").details == {"features": ["aio", "fs", "socket"], "default_features": False}
    assert _dep(version, "loom").details == {"features": ["futures", "checkpoint"]}


# --- reqwest ---------------------------------------------------------------------------------------------------


def _reqwest(serve):
    serve({f"{BASE}re/qw/reqwest": _index("reqwest", REQWEST_ETAG)})
    package = _fetch(_adapter(_registry()), "reqwest")
    assert package is not None
    return package


def test_reqwest_renamed_dependencies(serve) -> None:
    version = _version(_reqwest(serve), "0.13.5")
    renamed = _dep(version, "native-tls-crate")
    assert renamed.target == _ref("native-tls") and renamed.alias == "native-tls-crate"
    assert renamed.raw_spec == 'native-tls-crate = { package = "native-tls", version = "^0.2.16" }'
    assert renamed.constraint == "^0.2.16" and renamed.dependency_type == "optional"
    # The implicit feature is named after the local name, which `dep:native-tls-crate` hides.
    assert renamed.extra_name is None
    assert renamed.environment_markers == {
        "rust_cfg": 'cfg(not(all(target_arch = "wasm32", any(target_os = "unknown", target_os = "none"))))'}
    assert renamed.details is not None and "native-tls-vendored" in renamed.details["enabled_by"]

    dev = _dep(version, "brotli_crate")
    assert (dev.target, dev.alias, dev.dependency_type, dev.scope) == (_ref("brotli"), "brotli_crate", "dev", "dev")
    assert dev.raw_spec == 'brotli_crate = { package = "brotli", version = "^8" }'
    assert _dep(version, "zstd_crate").target == _ref("zstd")
    assert _dep(version, "serde").alias is None


def test_reqwest_a_weak_feature_does_not_turn_the_dependency_on(serve) -> None:
    version = _version(_reqwest(serve), "0.13.5")
    # `__rustls-aws-lc-rs` has `quinn?/rustls-aws-lc-rs`, but only `http3` has `dep:quinn`.
    assert _dep(version, "quinn").details == {
        "features": ["runtime-tokio"], "default_features": False, "enabled_by": ["http3"]}
    # `__native-tls-alpn` has `hyper-tls?/alpn`; the strong `dep:hyper-tls` is in `__native-tls`.
    hyper_tls = _dep(version, "hyper-tls").details
    assert hyper_tls is not None and "__native-tls-alpn" not in hyper_tls["enabled_by"]
    assert hyper_tls["enabled_by"] == [
        "__native-tls", "native-tls", "native-tls-no-alpn", "native-tls-vendored", "native-tls-vendored-no-alpn"]
    # `futures-channel` is named by `dep:` and by `futures-channel?/sink` in the same feature.
    assert _dep(version, "futures-channel").details == {"enabled_by": ["blocking"]}


def test_reqwest_a_feature_and_a_dependency_with_one_name(serve) -> None:
    # `rustls` is a feature (it includes `__rustls`) and the dependency `rustls` (`dep:rustls` in `__rustls`).
    rustls = _dep(_version(_reqwest(serve), "0.13.5"), "rustls")
    assert rustls.extra_name is None
    assert rustls.details == {
        "features": ["std", "tls12"], "default_features": False,
        "enabled_by": ["__rustls", "default", "default-tls", "http3", "rustls", "rustls-no-provider"]}


def test_link_cplusplus_links_and_build_dependencies(serve) -> None:
    serve({f"{BASE}li/nk/link-cplusplus": _index("link-cplusplus")})
    package = _fetch(_adapter(_registry()), "link-cplusplus")
    assert package is not None and len(package.versions) == 13
    newest = package.versions[-1]
    assert newest.raw_metadata == {"links": "cplusplus"} and newest.engines == {"rust": "1.68"}
    [cc] = newest.dependencies or []
    assert (cc.target, cc.dependency_type, cc.scope, cc.constraint) == (_ref("cc"), "build", "build", "^1")
    assert newest.features is not None and newest.features["libcxx"] == ["libc++"]
    assert all(v.raw_metadata in ({}, {"links": "cplusplus"}) for v in package.versions)


# --- the index: odd lines --------------------------------------------------------------------------------------


def _line(**changes: object) -> dict:
    line: dict[str, object] = {
        "name": "demo", "vers": "1.0.0", "deps": [], "cksum": "ab" * 32, "features": {}, "yanked": False,
        "pubtime": "2024-05-01T10:00:00Z",
    }
    return line | changes


def _entry_deps(*deps: dict, **entry: object) -> list[NormalizedDependency]:
    full = [{"name": "x", "req": "^1", "features": [], "optional": False, "default_features": True, "target": None,
             "kind": "normal"} | dep for dep in deps]
    return normalize_dependencies(_line(deps=full, **entry))


def test_a_version_the_scheme_rejects_is_skipped_and_logged(serve, caplog) -> None:
    body = "\n".join(json.dumps(_line(vers=v)) for v in ("1.0.0", "1.0", "v2.0.0", "1.1.0-beta.1")) + "\n"
    serve({f"{BASE}de/mo/demo": httpx.Response(200, text=body, headers={"etag": '"e"'})})
    with caplog.at_level(logging.WARNING):
        package = _fetch(_adapter(_registry()), "demo")
    assert package is not None and [v.version for v in package.versions] == ["1.0.0", "1.1.0-beta.1"]
    assert "'1.0'" in caplog.text and "'v2.0.0'" in caplog.text


def test_a_missing_date_or_checksum_is_not_an_error(serve) -> None:
    body = "\n".join(json.dumps(line) for line in (_line(pubtime=None), _line(vers="1.0.1", pubtime="soon", cksum=""))) + "\n"
    serve({f"{BASE}de/mo/demo": httpx.Response(200, text=body)})
    package = _fetch(_adapter(_registry()), "demo")
    assert package is not None and package.revision is None
    assert [v.release_date for v in package.versions] == [None, None]
    assert [v.artifacts for v in package.versions][1] == []
    assert package.versions[0].artifacts[0].checksums == {"sha256": "ab" * 32}


def test_the_display_name_is_the_published_spelling(serve) -> None:
    serve({f"{BASE}in/fl/inflector": httpx.Response(200, text=json.dumps(_line(name="Inflector")) + "\n")})
    package = _fetch(_adapter(_registry()), "Inflector")
    assert package is not None and package.display_name == "Inflector"
    assert package.versions[0].artifacts[0].filename == "Inflector-1.0.0.crate"
    assert package.versions[0].artifacts[0].download_url == "https://static.crates.io/crates/Inflector/1.0.0/download"


def test_dependency_kind_and_flags() -> None:
    deps = _entry_deps(
        {"name": "a"}, {"name": "b", "kind": None}, {"name": "c", "kind": "dev"}, {"name": "d", "kind": "build"},
        {"name": "e", "optional": True}, {"name": "f", "kind": "weird"},
    )
    assert [(d.raw_spec, d.scope, d.dependency_type) for d in deps] == [
        ('a = "^1"', "normal", "runtime"), ('b = "^1"', "normal", "runtime"), ('c = "^1"', "dev", "dev"),
        ('d = "^1"', "build", "build"), ('e = "^1"', "normal", "optional")]   # an unknown kind is skipped


def test_dependency_registry_and_features_go_to_details() -> None:
    [dep] = _entry_deps({"name": "x", "registry": "https://github.com/rust-lang/crates.io-index", "features": ["a"],
                         "default_features": False})
    assert dep.details == {"features": ["a"], "default_features": False,
                           "registry": "https://github.com/rust-lang/crates.io-index"}
    [plain] = _entry_deps({"name": "x", "registry": None})
    assert plain.details is None and plain.alias is None and plain.environment_markers is None


def test_a_dependency_that_cannot_be_a_crate_is_skipped_not_fatal(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        deps = _entry_deps({"name": "ok"}, {"name": "bad name!"}, {"name": "alias", "package": "wrong/name"}, {"name": "last"})
    assert [d.target.name for d in deps] == ["ok", "last"]
    assert "bad name!" in caplog.text and "wrong/name" in caplog.text


def test_a_target_triple_is_a_marker_too() -> None:
    [dep] = _entry_deps({"name": "x", "target": "x86_64-pc-windows-gnu"})
    assert dep.environment_markers == {"rust_cfg": "x86_64-pc-windows-gnu"}


# --- enabled_by ------------------------------------------------------------------------------------------------


def test_enabled_by_an_optional_dependency_is_its_own_implicit_feature() -> None:
    assert enabled_by({}, {"serde"}) == {"serde": ["serde"]}
    assert enabled_by({"derive": ["serde_derive"]}, {"serde_derive"}) == {"serde_derive": ["derive", "serde_derive"]}


def test_enabled_by_dep_syntax_removes_the_implicit_feature() -> None:
    features = {"json": ["dep:serde_json"]}
    assert enabled_by(features, {"serde_json"}) == {"serde_json": ["json"]}
    # ... unless the crate defines a feature of that name itself.
    assert enabled_by({"serde": ["dep:serde"], "all": ["serde"]}, {"serde"}) == {"serde": ["all", "serde"]}


def test_enabled_by_follows_features_transitively() -> None:
    features = {"default": ["std"], "std": ["alloc"], "alloc": ["dep:hashbrown"], "other": []}
    assert enabled_by(features, {"hashbrown"}) == {"hashbrown": ["alloc", "default", "std"]}


def test_enabled_by_survives_a_cycle() -> None:
    features = {"a": ["b"], "b": ["c"], "c": ["a", "dep:x"], "d": ["d", "a"]}
    assert enabled_by(features, {"x"}) == {"x": ["a", "b", "c", "d"]}


def test_enabled_by_a_strong_dependency_feature_turns_the_dependency_on() -> None:
    assert enabled_by({"net": ["mio/os-poll"], "rt": ["tokio/rt"]}, {"mio"}) == {"mio": ["mio", "net"]}
    # `x/feat` of a dependency that is not optional (or not there) switches nothing on.
    assert enabled_by({"rt": ["tokio/rt"]}, {"mio"}) == {"mio": ["mio"]}


def test_enabled_by_a_weak_dependency_feature_does_not() -> None:
    features = {"tls": ["dep:rustls", "hyper?/tls"], "alpn": ["rustls?/alpn", "hyper?/alpn"]}
    assert enabled_by(features, {"rustls", "hyper"}) == {"hyper": ["hyper"], "rustls": ["tls"]}


def test_enabled_by_a_bare_name_is_a_feature_when_there_is_one_else_the_old_spelling_of_dep() -> None:
    # `std` is a feature; `libc` is only a dependency (old syntax: the name of the optional dependency).
    features = {"full": ["std", "libc"], "std": []}
    assert enabled_by(features, {"libc"}) == {"libc": ["full", "libc"]}
    # With `dep:libc` elsewhere there is no implicit feature, and a bare `libc` is still the dependency.
    assert enabled_by({"full": ["libc"], "x": ["dep:libc"]}, {"libc"}) == {"libc": ["full", "x"]}


def test_enabled_by_ignores_what_is_not_there() -> None:
    assert enabled_by({"a": ["dep:ghost", "ghost/feat", "nothing"]}, {"x"}) == {"x": ["x"]}
    assert enabled_by({"a": []}, set()) == {}


def test_enabled_by_default_is_a_feature_like_any_other() -> None:
    assert enabled_by({"default": ["dep:log"]}, {"log"}) == {"log": ["default"]}


# --- missing crates and the API failing ------------------------------------------------------------------------


def test_a_missing_crate_is_none_and_asks_nothing_else(serve, turns) -> None:
    upstream = serve({})
    assert _fetch(_adapter(), "serde-json") is None
    assert upstream.urls == [f"{BASE}se/rd/serde-json"] and turns == []


@pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(403, text="forbidden"), httpx.Response(404, json={}),
                                    httpx.Response(200, text="<html>"), httpx.Response(200, json=[1])])
def test_a_failing_api_leaves_the_package_fields_empty(serve, caplog, answer) -> None:
    serve({f"{BASE}se/rd/serde": _index("serde", SERDE_ETAG), f"{API}/api/v1/crates/serde": answer})
    with caplog.at_level(logging.WARNING):
        package = _fetch(_adapter(), "serde")
    assert package is not None and len(package.versions) == 13 and package.revision == SERDE_ETAG
    assert (package.description, package.homepage_url, package.repository_url, package.documentation_url) == (None,) * 4
    assert (package.license_raw, package.license_spdx, package.keywords) == (None, None, None)
    assert (package.downloads_total, package.downloads_recent, package.created_at, package.updated_at) == (None,) * 4
    assert "crates.io API" in caplog.text


def test_the_api_is_retried_and_waits_its_turn_each_time(serve, turns) -> None:
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts < 3 else _api("api-serde.json")

    upstream = serve({f"{BASE}se/rd/serde": _index("serde"), f"{API}/api/v1/crates/serde": flaky})
    adapter = _adapter()
    adapter.max_retries, adapter.retry_base_seconds, adapter.retry_jitter_seconds = 3, 0.0, 0.0
    package = _fetch(adapter, "serde")
    assert package is not None and package.description is not None
    assert upstream.urls.count(f"{API}/api/v1/crates/serde") == 3
    assert turns == [("crates-io:api", 1.0)] * 3   # one turn per request that reached the API


def test_the_api_gives_up_after_the_retries(serve, turns) -> None:
    upstream = serve({f"{BASE}se/rd/serde": _index("serde"), f"{API}/api/v1/crates/serde": httpx.Response(500)})
    adapter = _adapter()
    adapter.max_retries, adapter.retry_base_seconds, adapter.retry_jitter_seconds = 2, 0.0, 0.0
    package = _fetch(adapter, "serde")
    assert package is not None and package.description is None
    assert upstream.urls.count(f"{API}/api/v1/crates/serde") == 3 and len(turns) == 3


def test_the_index_is_not_rate_limited(serve, turns) -> None:
    serve({f"{BASE}se/rd/serde": _index("serde"), f"{API}/api/v1/crates/serde": _api("api-serde.json")})
    adapter = _adapter()
    asyncio.run(adapter.exists(_ref("serde")))
    _deps(adapter, "serde", "1.0.229")
    asyncio.run(adapter.test_connection())
    assert turns == []


def test_without_api_url_there_is_no_api_request(serve, turns) -> None:
    upstream = serve({f"{BASE}se/rd/serde": _index("serde")})
    for registry in (_registry(), _registry(api_url=""), _registry(api_url=None)):
        assert _fetch(_adapter(registry), "serde") is not None
    assert all(url.startswith(BASE) for url in upstream.urls) and turns == []


def test_the_config_is_read_once_per_adapter(serve) -> None:
    upstream = serve({f"{BASE}se/rd/serde": _index("serde"), f"{BASE}to/ki/tokio": _index("tokio")})
    adapter = _adapter(_registry())
    _fetch(adapter, "serde")
    _fetch(adapter, "tokio")
    assert upstream.urls.count(f"{BASE}config.json") == 1
    _fetch(_adapter(_registry()), "serde")
    assert upstream.urls.count(f"{BASE}config.json") == 2


# --- dl --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("dl", "name", "expected"), [
    ("https://static.crates.io/crates", "serde", "https://static.crates.io/crates/serde/1.2.3/download"),
    ("https://static.crates.io/crates/", "serde", "https://static.crates.io/crates/serde/1.2.3/download"),
    ("https://dl.example/{crate}/{version}.crate", "serde", "https://dl.example/serde/1.2.3.crate"),
    ("https://dl.example/{prefix}/{crate}-{version}.crate", "serde", "https://dl.example/se/rd/serde-1.2.3.crate"),
    ("https://dl.example/{prefix}/{crate}", "Inflector", "https://dl.example/In/fl/Inflector"),
    ("https://dl.example/{lowerprefix}/{crate}", "Inflector", "https://dl.example/in/fl/Inflector"),
    ("https://dl.example/{prefix}/{crate}", "syn", "https://dl.example/3/s/syn"),
    ("https://dl.example/{prefix}/{crate}", "cc", "https://dl.example/2/cc"),
    ("https://dl.example/{prefix}/{crate}", "a", "https://dl.example/1/a"),
    ("https://dl.example/{sha256-checksum}.crate", "serde", "https://dl.example/abc123.crate"),
    ("https://dl.example/{crate}/{crate}-{version}.crate", "serde", "https://dl.example/serde/serde-1.2.3.crate"),
])
def test_download_url_follows_the_dl_rule(dl: str, name: str, expected: str) -> None:
    assert download_url(dl, name, "1.2.3", "abc123") == expected


def test_the_artifact_url_uses_the_dl_of_the_registry(serve) -> None:
    config = {"dl": "https://dl.corp.example/{prefix}/{crate}/{crate}-{version}.crate", "api": "https://crates.corp.example"}
    serve({f"{BASE}config.json": httpx.Response(200, json=config), f"{BASE}se/rd/serde": _index("serde")})
    package = _fetch(_adapter(_registry()), "serde")
    assert package is not None
    assert package.versions[-1].artifacts[0].download_url == "https://dl.corp.example/se/rd/serde/serde-1.0.229.crate"


# --- fetch_version_dependencies, exists, test_connection, canonical_name --------------------------------------


def test_fetch_version_dependencies(serve) -> None:
    upstream = serve({f"{BASE}to/ki/tokio": _index("tokio")})
    adapter = _adapter(_registry())
    result = _deps(adapter, "tokio", "1.54.1")
    assert upstream.urls == [f"{BASE}to/ki/tokio"]   # no config.json, no API: only the file
    assert result.available and result.engines == {"rust": "1.85"}
    assert [d.raw_spec for d in result.dependencies][:2] == ['async-stream = "^0.3"', 'backtrace = "^0.3.58"']
    assert result.dependencies == _version(_tokio(serve), "1.54.1").dependencies
    old = _deps(adapter, "tokio", "0.0.0")
    assert old.available and old.dependencies == [] and old.engines is None


def test_fetch_version_dependencies_of_something_missing(serve) -> None:
    serve({f"{BASE}to/ki/tokio": _index("tokio")})
    adapter = _adapter(_registry())
    for result in (_deps(adapter, "tokio", "9.9.9"), _deps(adapter, "nope", "1.0.0")):
        assert result.available is False and result.dependencies == [] and result.engines is None


def test_exists(serve) -> None:
    serve({f"{BASE}se/rd/serde": _index("serde")})
    adapter = _adapter()
    assert asyncio.run(adapter.exists(_ref("serde"))) is True
    assert asyncio.run(adapter.exists(_ref("serde-json"))) is False


def test_exists_raises_on_a_server_error(serve) -> None:
    serve({f"{BASE}se/rd/serde": httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().exists(_ref("serde")))


def test_test_connection_reads_config_json(serve) -> None:
    upstream = serve({})
    asyncio.run(_adapter().test_connection())
    assert upstream.urls == [f"{BASE}config.json"]


def test_test_connection_fails_on_an_error_status(serve) -> None:
    serve({f"{BASE}config.json": httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())
    serve({f"{BASE}config.json": httpx.Response(404)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())


@pytest.mark.parametrize("answer", [
    httpx.Response(200, json={"api": "https://crates.io"}), httpx.Response(200, json={"dl": 3}),
    httpx.Response(200, json=["dl"]), httpx.Response(200, text="<html>not a registry</html>"),
])
def test_test_connection_fails_on_something_that_is_not_a_config(serve, answer) -> None:
    serve({f"{BASE}config.json": answer})
    with pytest.raises(ValueError):
        asyncio.run(_adapter().test_connection())


def test_canonical_name(serve, turns) -> None:
    upstream = serve({f"{API}/api/v1/crates/serde-json?include=": _api("api-serde-json-include.json")})
    adapter = _adapter()
    assert asyncio.run(adapter.canonical_name("serde-json")) == "serde_json"
    assert upstream.urls == [f"{API}/api/v1/crates/serde-json?include="]
    assert turns == [("crates-io:api", 1.0)]
    assert asyncio.run(adapter.canonical_name("  serde-json ")) == "serde_json"


def test_canonical_name_of_an_unknown_crate_or_without_an_api(serve, turns) -> None:
    upstream = serve({})
    assert asyncio.run(_adapter().canonical_name("nope-nope")) is None
    assert len(turns) == 1
    assert asyncio.run(_adapter(_registry()).canonical_name("serde-json")) is None
    assert upstream.urls == [f"{API}/api/v1/crates/nope-nope?include="] and len(turns) == 1


def test_canonical_name_raises_on_an_api_error(serve) -> None:
    serve({f"{API}/api/v1/crates/serde?include=": httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().canonical_name("serde"))


# --- the change detector ---------------------------------------------------------------------------------------


def _tracked(raw: str, revision: str | None, etag: str | None = None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(ref=_ref(raw), package_id=raw, revision=revision, etag=etag, unavailable=unavailable)


def _conditional(etag: str, new_etag: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == etag and new_etag is None:
            return httpx.Response(304, headers={"etag": etag})
        return _index("serde", etag=new_etag or etag)

    return answer


def test_the_detector_is_the_etag_one() -> None:
    assert isinstance(_adapter().change_detector(), CratesEtagChangeDetector)


def test_detector_asks_conditionally_and_reports_changed_and_missing(serve, turns) -> None:
    upstream = serve({
        f"{BASE}2/sa": _conditional('"a"'),
        f"{BASE}se/rd/serde": _conditional('"old"', new_etag='"new"'),
        f"{BASE}to/ki/tokio": httpx.Response(404),
        f"{BASE}3/s/syn": _conditional('"b"'),
        f"{BASE}cc/cc/cccc": httpx.Response(404),
        f"{BASE}re/qw/reqwest": _conditional('"a"'),
        f"{BASE}in/fl/inflector": _conditional('"c"'),
    })
    tracked = [
        _tracked("sa", '"a"'), _tracked("serde", '"old"'), _tracked("tokio", '"g"'), _tracked("never-synced", None),
        _tracked("syn", '"b"', unavailable=True), _tracked("cccc", '"s"', unavailable=True),
        _tracked("reqwest", None, etag='"a"'), _tracked("Inflector", '"c"'),
    ]

    result = asyncio.run(_adapter().change_detector().detect(tracked, {"anything": 1}))

    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("serde", '"new"'), ("never-synced", None), ("syn", '"b"')]
    assert [p.ref.name for p in result.missing] == ["tokio"]
    assert result.cursor == {} and not result.reconcile_all
    sent = {str(r.url).removeprefix(BASE): r.headers.get("if-none-match") for r in upstream.requests}
    assert sent["2/sa"] == '"a"' and sent["se/rd/serde"] == '"old"'
    assert sent["re/qw/reqwest"] == '"a"' and sent["in/fl/inflector"] == '"c"'   # the ETag column when there is no revision
    assert "ne/ve/never-synced" not in sent   # without a stored ETag there is nothing to ask conditionally
    assert turns == []   # the index is not the API


def test_detector_bounds_the_concurrency(monkeypatch) -> None:
    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(304, headers={"etag": '"a"'})

    monkeypatch.setattr(sparse, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    tracked = [_tracked(f"crate{i}", '"a"') for i in range(8)]
    result = asyncio.run(CratesEtagChangeDetector(_adapter(), concurrency=3).detect(tracked, {}))
    assert result.changed == [] and result.missing == [] and peak == 3


def test_detector_survives_one_failing_package(serve) -> None:
    serve({f"{BASE}se/rd/serde": _conditional('"old"', new_etag='"new"'), f"{BASE}to/ki/tokio": httpx.Response(503)})
    result = asyncio.run(_adapter().change_detector().detect([_tracked("serde", '"old"'), _tracked("tokio", '"b"')], {}))
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("serde", '"new"')]
    assert result.missing == []
