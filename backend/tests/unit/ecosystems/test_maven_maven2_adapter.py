"""The `maven2` adapter (WP J4) on answers recorded from Maven Central on 2026-10-30 with
`curl -sS -A "PackageHub/0.1 (+https://github.com/nyrkovms/RepoGraph)" -D <headers> -o <body>
https://repo1.maven.org/maven2/<path>` (tests/fixtures/maven/maven2/<path>; `headers.json` keeps the ETag,
Last-Modified and Content-Type of each file):

- org/springframework/spring-core: `maven-metadata.xml` (338 versions, `<release>` 7.1.0-RC1) and the POM of 7.1.0-RC1
  (the release; carries Gradle's marker), 7.0.10, 6.2.0, 6.1.14 (depends on spring-jcl) and 5.3.39. Only these five
  answer HEAD in the fake: the other versions answer 404, which is how the tests see a version without a date;
- com/fasterxml/jackson/core/jackson-databind 2.17.2 with what the effective model loads: jackson-base 2.17.2
  (parent), jackson-bom 2.17.2 (imported by jackson-base), jackson-parent 2.17, oss-parent 58 and junit-bom 5.10.2;
- com/google/guava/guava 33.3.1-jre and its parent guava-parent (versions come from the parent's dependencyManagement);
- mysql/mysql-connector-java 8.0.33: a POM that is only a relocation to com.mysql:mysql-connector-j;
- org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm 1.9.0: published with Gradle metadata.
Files were recorded whole. Everything is served by a fake upstream; nothing here reaches the network."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.ecosystems.base import (
    FetchedDocument,
    NormalizedAuthor,
    NormalizedPackage,
    NormalizedVersion,
    PackageRef,
    RegistryConfig,
    TrackedPackage,
)
from app.ecosystems.maven.adapters import Maven2Adapter, Maven2ChangeDetector
from app.ecosystems.maven.adapters import maven2
from app.ecosystems.maven.adapters.maven2 import (
    dependencies_of,
    kept_versions,
    metadata_revision,
    parse_metadata,
    pick_latest,
)
from app.ecosystems.maven.coordinates import MavenCoordinates
from app.ecosystems.maven.pom import effective_model, parse_pom

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "maven" / "maven2"
HEADERS: dict[str, dict[str, str]] = json.loads((FIXTURES / "headers.json").read_text())
BASE = "https://repo1.maven.org/maven2/"
OTHER = "https://mirror.example.org/maven/"
Route = httpx.Response | Callable[[httpx.Request], httpx.Response]

SPRING = "org.springframework:spring-core"
JACKSON = "com.fasterxml.jackson.core:jackson-databind"
SPRING_METADATA = "org/springframework/spring-core/maven-metadata.xml"
SPRING_RELEASE = "7.1.0-RC1"
JACKSON_POM = "com/fasterxml/jackson/core/jackson-databind/2.17.2/jackson-databind-2.17.2.pom"
JACKSON_PARENTS = [
    "com/fasterxml/jackson/jackson-base/2.17.2/jackson-base-2.17.2.pom",
    "com/fasterxml/jackson/jackson-bom/2.17.2/jackson-bom-2.17.2.pom",
    "com/fasterxml/jackson/jackson-parent/2.17/jackson-parent-2.17.pom",
    "com/fasterxml/oss-parent/58/oss-parent-58.pom",
    "org/junit/junit-bom/5.10.2/junit-bom-5.10.2.pom",
]


def _registry(base_url: str = BASE, registry_id: str = "reg-central") -> RegistryConfig:
    return RegistryConfig(
        id=registry_id, slug="central", ecosystem="maven", protocol="maven2", base_url=base_url,
        priority=0, config={}, routing=None, auth_type="none", credentials_ref=None,
    )


def _ref(raw: str) -> PackageRef:
    return MavenCoordinates().parse(raw)


def _recorded(path: str) -> bytes:
    return (FIXTURES / path).read_bytes()


def _xml(body: str) -> str:
    return f'<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">{body}</project>'


def _metadata(versions: Sequence[str], release: str | None = None, last_updated: str | None = None) -> str:
    return (
        "<metadata><groupId>g</groupId><artifactId>a</artifactId><versioning>"
        + (f"<release>{release}</release>" if release else "")
        + "<versions>" + "".join(f"<version>{v}</version>" for v in versions) + "</versions>"
        + (f"<lastUpdated>{last_updated}</lastUpdated>" if last_updated else "")
        + "</versioning></metadata>"
    )


class Upstream:
    """A fake repository: explicit routes first, then the recorded files under the same path (GET with the recorded
    ETag; HEAD only for the files that have recorded headers), else 404."""

    def __init__(self, routes: dict[str, Route], hosts: Sequence[str]) -> None:
        self.routes = routes
        self.hosts = hosts
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is not None:
            return route(request) if callable(route) else route
        path = request.url.path.removeprefix("/maven2/").removeprefix("/maven/")
        file = FIXTURES / path
        if request.url.host in self.hosts and path in HEADERS and file.is_file():
            recorded = HEADERS[path]
            headers = {"etag": recorded["etag"], "last-modified": recorded["last-modified"],
                       "content-type": recorded["content-type"]}
            if request.method == "HEAD":
                return httpx.Response(200, headers=headers)
            return httpx.Response(200, content=file.read_bytes(), headers=headers)
        return httpx.Response(404, text="not found")

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def count(self, method: str) -> int:
        return sum(request.method == method for request in self.requests)


@pytest.fixture(autouse=True)
def _fresh_pom_cache():
    maven2._POM_CACHE.clear()
    yield
    maven2._POM_CACHE.clear()


@pytest.fixture
def serve(monkeypatch):
    def start(routes: dict[str, Route] | None = None, hosts: Sequence[str] = ("repo1.maven.org",)) -> Upstream:
        upstream = Upstream(routes or {}, hosts)
        monkeypatch.setattr(
            maven2, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
        )
        return upstream

    return start


class FakeLookup:
    def __init__(self, *adapters: Maven2Adapter) -> None:
        self.adapters = adapters
        self.asked: list[PackageRef] = []

    def adapters_for(self, ref: PackageRef):
        self.asked.append(ref)
        return list(self.adapters)


def _adapter(base_url: str = BASE, registry_id: str = "reg-central") -> Maven2Adapter:
    adapter = Maven2Adapter(_registry(base_url, registry_id))
    adapter.max_retries = 0   # a 5xx in a test is an answer, not something to wait out
    return adapter


def _fetch(raw: str, adapter: Maven2Adapter | None = None, lookup: FakeLookup | None = None) -> NormalizedPackage | None:
    return asyncio.run((adapter or _adapter()).fetch_package(_ref(raw), lookup))  # type: ignore[arg-type]


def _dependencies(raw: str, version: str, adapter: Maven2Adapter | None = None, lookup: FakeLookup | None = None):
    return asyncio.run((adapter or _adapter()).fetch_version_dependencies(_ref(raw), version, lookup))  # type: ignore[arg-type]


def _version(package: NormalizedPackage, number: str) -> NormalizedVersion:
    return next(v for v in package.versions if v.version == number)


def _pom_url(raw: str, version: str, base: str = BASE) -> str:
    group, artifact = raw.split(":")
    return f"{base}{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"


def _metadata_url(raw: str, base: str = BASE) -> str:
    group, artifact = raw.split(":")
    return f"{base}{group.replace('.', '/')}/{artifact}/maven-metadata.xml"


def _model(body: str):
    async def nothing(group: str, artifact: str, version: str):
        return None

    return asyncio.run(effective_model(parse_pom(_xml(
        f"<groupId>g</groupId><artifactId>a</artifactId><version>1</version>{body}")), nothing))


# --- maven-metadata.xml -----------------------------------------------------------------------------------------


def test_metadata_is_parsed() -> None:
    metadata = parse_metadata(_recorded(SPRING_METADATA), "u")
    assert metadata.release == metadata.latest == SPRING_RELEASE
    assert metadata.last_updated == "20261022075448"
    assert len(metadata.versions) == 338 and metadata.versions[:3] == ["1.0-rc1", "1.0", "1.0.1"]


def test_metadata_tolerates_a_namespace_and_missing_parts() -> None:
    document = ('<metadata xmlns="http://maven.apache.org/METADATA/1.1.0"><versioning><versions>'
                "<version>1</version></versions></versioning></metadata>")
    metadata = parse_metadata(document, "u")
    assert (metadata.versions, metadata.release, metadata.latest, metadata.last_updated) == (["1"], None, None, None)
    assert parse_metadata("<metadata><groupId>g</groupId></metadata>", "u").versions == []


@pytest.mark.parametrize("document", ["<metadata>", "", "<html><body>login</body></html>"])
def test_unparseable_metadata_raises_with_the_url(document: str) -> None:
    with pytest.raises(ValueError, match="https://x/maven-metadata.xml"):
        parse_metadata(document, "https://x/maven-metadata.xml")


def test_versions_are_deduplicated_in_order_without_snapshots_and_invalid(caplog) -> None:
    ref = _ref("g:a")
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        kept = kept_versions(["1.0", "2.0-SNAPSHOT", "1.1", "1.0", "bad version", "", "2.0"], ref)
    assert kept == ["1.0", "1.1", "2.0"]
    assert "'bad version'" in caplog.text and "SNAPSHOT" not in caplog.text


def test_revision_is_the_etag_then_last_updated_then_a_hash() -> None:
    metadata = parse_metadata(_metadata(["2.0", "1.0"], last_updated="20260101000000"), "u")
    assert metadata_revision('"abc"', metadata, metadata.versions) == '"abc"'
    assert metadata_revision('W/"abc"', metadata, metadata.versions) == 'W/"abc"'
    assert metadata_revision(None, metadata, metadata.versions) == "lastUpdated:20260101000000"
    bare = parse_metadata(_metadata(["2.0", "1.0"]), "u")
    assert metadata_revision(None, bare, bare.versions) == hashlib.sha256(b"1.0\n2.0").hexdigest()
    assert metadata_revision(None, bare, ["1.0", "2.0"]) == metadata_revision(None, bare, ["2.0", "1.0"])


def test_the_latest_is_the_release_else_the_highest_release_else_the_highest() -> None:
    assert pick_latest("1.0", ["1.0", "2.0"]) == "1.0"
    assert pick_latest("3.0", ["1.0", "2.0", "2.1-beta-1"]) == "2.0"          # <release> is not among the versions
    assert pick_latest(None, ["1.0", "1.10", "1.9"]) == "1.10"
    assert pick_latest(None, ["1.0-rc1", "1.0-rc2"]) == "1.0-rc2"
    assert pick_latest(None, []) is None


@pytest.mark.parametrize("variant", ["etag", "last_updated", "hash"])
def test_fetch_package_stores_the_revision_by_what_the_repository_sends(serve, variant: str) -> None:
    document = _metadata(["1.0", "1.1"], release="1.1", last_updated="20260101000000" if variant != "hash" else None)
    headers = {"etag": '"e1"'} if variant == "etag" else {}
    serve({_metadata_url("g:a"): httpx.Response(200, text=document, headers=headers)})
    package = _fetch("g:a")
    assert package is not None
    assert package.revision == {"etag": '"e1"', "last_updated": "lastUpdated:20260101000000",
                                "hash": hashlib.sha256(b"1.0\n1.1").hexdigest()}[variant]


# --- fetch_package: spring-core ----------------------------------------------------------------------------------


def test_spring_core_versions_latest_and_package_fields(serve) -> None:
    upstream = serve()
    package = _fetch(SPRING)
    assert package is not None
    listed = parse_metadata(_recorded(SPRING_METADATA), "u").versions
    assert [v.version for v in package.versions] == listed
    assert package.revision == HEADERS[SPRING_METADATA]["etag"]
    assert package.display_name == SPRING and package.registry_id == "reg-central"
    assert package.dist_tags == {"latest": SPRING_RELEASE}
    assert package.description == "Spring Core"
    assert package.homepage_url == package.repository_url == "https://github.com/spring-projects/spring-framework"
    assert package.license_raw == "Apache License, Version 2.0" and package.license_spdx == "Apache-2.0"
    assert package.authors == [NormalizedAuthor("Juergen Hoeller", "juergen.hoeller@broadcom.com", None, "author")]
    assert package.deprecation is None
    assert all(v.status == "active" for v in package.versions)
    # The metadata, the POM of the latest and one HEAD per version: no other POM is read.
    assert upstream.count("GET") == 2 and upstream.count("HEAD") == len(listed)


def test_spring_core_latest_has_dependencies_and_artifacts_the_others_are_lazy(serve) -> None:
    serve()
    package = _fetch(SPRING)
    assert package is not None
    latest = _version(package, SPRING_RELEASE)
    assert [(d.target.namespace + ":" + d.target.name, d.constraint, d.dependency_type, d.scope, d.raw_spec)
            for d in latest.dependencies or []] == [
        ("commons-logging:commons-logging", "1.3.5", "runtime", "compile", "commons-logging:commons-logging:1.3.5"),
        ("org.jspecify:jspecify", "1.0.1", "runtime", "compile", "org.jspecify:jspecify:1.0.1"),
    ]
    jar = BASE + "org/springframework/spring-core/7.1.0-RC1/spring-core-7.1.0-RC1"
    assert [(a.filename, a.artifact_type, a.download_url) for a in latest.artifacts] == [
        ("spring-core-7.1.0-RC1.pom", "pom", jar + ".pom"), ("spring-core-7.1.0-RC1.jar", "jar", jar + ".jar")]
    assert all(v.dependencies is None and v.raw_metadata == {} for v in package.versions if v is not latest)
    other = _version(package, "6.1.14")
    # Without their POM the packaging of the other versions is unknown: a .pom and a .jar.
    assert [a.artifact_type for a in other.artifacts] == ["pom", "jar"]
    assert not any(a.has_metadata_file for a in other.artifacts)


def test_spring_core_latest_raw_metadata_and_the_gradle_marker(serve) -> None:
    serve()
    package = _fetch(SPRING)
    assert package is not None
    latest = _version(package, SPRING_RELEASE)
    assert latest.raw_metadata == {"packaging": "jar", "managed": [], "gradle_metadata": True}
    assert [(a.artifact_type, a.has_metadata_file) for a in latest.artifacts] == [("pom", True), ("jar", False)]


def test_release_dates_come_from_the_last_modified_of_the_pom(serve) -> None:
    serve()
    package = _fetch(SPRING)
    assert package is not None
    assert _version(package, SPRING_RELEASE).release_date == datetime(2026, 10, 22, 7, 46, 54, tzinfo=UTC)
    assert _version(package, "6.1.14").release_date == datetime(2024, 10, 17, 8, 10, 0, tzinfo=UTC)
    assert _version(package, "5.3.39").release_date == datetime(2024, 8, 14, 9, 4, 42, tzinfo=UTC)
    dated = {v.version for v in package.versions if v.release_date is not None}
    assert dated == {SPRING_RELEASE, "7.0.10", "6.2.0", "6.1.14", "5.3.39"}   # the others answered 404 to HEAD
    assert len(package.versions) == 338                                     # a version without a date is not lost


def test_a_failing_head_or_a_bad_date_leaves_none_and_keeps_the_version(serve, caplog) -> None:
    serve({
        _metadata_url("g:a"): httpx.Response(200, text=_metadata(["1.0", "1.1", "1.2", "1.3"], release="1.3")),
        _pom_url("g:a", "1.0"): httpx.Response(200, headers={"last-modified": "Wed, 01 Jan 2025 10:00:00 GMT"}),
        _pom_url("g:a", "1.1"): httpx.Response(503),
        _pom_url("g:a", "1.2"): httpx.Response(200, headers={"last-modified": "not a date"}),
        _pom_url("g:a", "1.3"): httpx.Response(200, headers={}),
    })
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        package = _fetch("g:a")
    assert package is not None
    assert {v.version: v.release_date for v in package.versions} == {
        "1.0": datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "1.1": None, "1.2": None, "1.3": None}
    assert "No release date for g:a@1.1" in caplog.text


def test_head_requests_are_bounded_by_the_concurrency(monkeypatch) -> None:
    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        if request.url.path.endswith("maven-metadata.xml"):
            return httpx.Response(200, text=_metadata([f"1.{i}" for i in range(12)], release="1.0"))
        if request.method == "GET":
            return httpx.Response(404)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, headers={"last-modified": "Wed, 01 Jan 2025 10:00:00 GMT"})

    monkeypatch.setattr(maven2, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    adapter = _adapter()
    adapter.concurrency = 3
    package = asyncio.run(adapter.fetch_package(_ref("g:a"), None))  # type: ignore[arg-type]
    assert package is not None and peak == 3 and all(v.release_date is not None for v in package.versions)


def test_the_release_is_the_latest_only_when_it_is_among_the_versions(serve) -> None:
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1.0", "1.5", "2.0-SNAPSHOT", "2.0-rc1"], release="2.0-SNAPSHOT"))})
    package = _fetch("g:a")
    assert package is not None
    assert [v.version for v in package.versions] == ["1.0", "1.5", "2.0-rc1"]
    # <release> was dropped as a SNAPSHOT: the highest release stands in, and no tag says "release".
    assert package.dist_tags is None and _version(package, "1.5").artifacts[-1].artifact_type == "jar"


def test_an_artifact_with_no_versions_still_exists(serve) -> None:
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata([]), headers={"etag": '"x"'})})
    package = _fetch("g:a")
    assert package is not None and package.versions == [] and package.dist_tags is None and package.revision == '"x"'


def test_missing_metadata_is_no_package(serve) -> None:
    upstream = serve()
    assert _fetch("org.example:nothing") is None
    serve({_metadata_url("g:a"): httpx.Response(410)})
    assert _fetch("g:a") is None
    assert upstream.count("HEAD") == 0


def test_unparseable_metadata_fails_the_package(serve) -> None:
    serve({_metadata_url("g:a"): httpx.Response(200, text="<html>")})
    with pytest.raises(ValueError, match="maven-metadata.xml"):
        _fetch("g:a")


def test_a_server_error_on_the_metadata_raises(serve) -> None:
    serve({_metadata_url("g:a"): httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        _fetch("g:a")


# --- the latest POM that is missing or broken --------------------------------------------------------------------


def test_a_missing_latest_pom_keeps_the_versions_without_its_data(serve, caplog) -> None:
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1.0", "1.1"], release="1.1"))})
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        package = _fetch("g:a")
    assert package is not None and [v.version for v in package.versions] == ["1.0", "1.1"]
    assert all(v.dependencies is None and v.raw_metadata == {} for v in package.versions)
    assert [a.artifact_type for a in _version(package, "1.1").artifacts] == ["pom", "jar"]
    assert package.description is None and package.license_raw is None and package.authors is None
    assert package.dist_tags == {"latest": "1.1"} and "g:a@1.1 has no POM" in caplog.text


def test_an_unreadable_latest_pom_keeps_the_versions_without_its_data(serve, caplog) -> None:
    serve({
        _metadata_url("g:a"): httpx.Response(200, text=_metadata(["1.0"], release="1.0")),
        _pom_url("g:a", "1.0"): httpx.Response(200, text="<project><groupId>g</project>"),
    })
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        package = _fetch("g:a")
    assert package is not None and _version(package, "1.0").dependencies is None
    assert "Unreadable POM of g:a@1.0" in caplog.text


def test_a_server_error_on_the_latest_pom_is_a_source_error(serve) -> None:
    serve({
        _metadata_url("g:a"): httpx.Response(200, text=_metadata(["1.0"], release="1.0")),
        _pom_url("g:a", "1.0"): httpx.Response(502),
    })
    with pytest.raises(httpx.HTTPStatusError):
        _fetch("g:a")


# --- the effective model through `load` -------------------------------------------------------------------------


def test_jackson_databind_gets_its_versions_from_the_parent_and_the_bom(serve) -> None:
    serve()
    result = _dependencies(JACKSON, "2.17.2")
    assert result.available
    by_name = {d.target.name: d for d in result.dependencies}
    assert by_name["jackson-annotations"].constraint == "2.17.2"      # jackson-base's property, from the BOM
    assert by_name["jackson-core"].constraint == "2.17.2"
    assert all(d.constraint and "${" not in d.constraint for d in result.dependencies)
    assert all("unresolved_property" not in (d.details or {}) for d in result.dependencies)


def test_own_repository_poms_are_returned_as_documents(serve) -> None:
    serve()
    result = _dependencies(JACKSON, "2.17.2")
    assert [d.path for d in result.documents][0] == JACKSON_POM          # the version first, then what it loaded
    assert sorted(d.path for d in result.documents) == sorted([JACKSON_POM, *JACKSON_PARENTS])
    assert all(d.content_type == "application/xml" and d.content == _recorded(d.path) for d in result.documents)
    assert {d.path: d.etag for d in result.documents} == {path: HEADERS[path]["etag"] for path in [JACKSON_POM, *JACKSON_PARENTS]}


def test_guava_takes_versions_from_its_parent(serve) -> None:
    serve()
    result = _dependencies("com.google.guava:guava", "33.3.1-jre")
    by_name = {d.target.name: d.constraint for d in result.dependencies}
    assert by_name["failureaccess"] == "1.0.2"
    assert by_name["jsr305"] and "${" not in by_name["jsr305"]
    assert [d.path for d in result.documents] == [
        "com/google/guava/guava/33.3.1-jre/guava-33.3.1-jre.pom",
        "com/google/guava/guava-parent/33.3.1-jre/guava-parent-33.3.1-jre.pom"]


def test_a_second_load_is_served_by_the_module_cache(serve) -> None:
    upstream = serve()
    first = _dependencies(JACKSON, "2.17.2")
    assert upstream.urls.count(BASE + JACKSON_PARENTS[0]) == 1 and len(upstream.requests) == 1 + len(JACKSON_PARENTS)
    upstream.requests.clear()
    second = _dependencies(JACKSON, "2.17.2")
    assert upstream.urls == [BASE + JACKSON_POM]                         # only the version POM itself is read again
    assert second.dependencies == first.dependencies
    assert sorted(d.path for d in second.documents) == sorted(d.path for d in first.documents)   # still handed back


def test_the_cache_is_per_repository_and_bounded(serve) -> None:
    serve()
    _dependencies(JACKSON, "2.17.2")
    mirror = _adapter(OTHER, "reg-mirror")
    upstream = serve(hosts=("mirror.example.org",))
    _dependencies(JACKSON, "2.17.2", mirror)
    assert len(upstream.requests) == 1 + len(JACKSON_PARENTS)           # another base url is not served from the first
    cache = maven2._PomCache(2)
    for index in range(3):
        cache.put(("b", str(index)), b"x", None)
    assert len(cache) == 2 and cache.get(("b", "0")) is None and cache.get(("b", "2")) is not None
    cache.get(("b", "1"))
    cache.put(("b", "3"), b"x", None)
    assert cache.get(("b", "2")) is None and cache.get(("b", "1")) is not None   # least recently used goes first


def test_a_parent_missing_here_is_loaded_from_another_repository_and_not_returned(serve) -> None:
    parent = JACKSON_PARENTS[0]
    upstream = serve({BASE + parent: httpx.Response(404)}, hosts=("repo1.maven.org", "mirror.example.org"))
    central, mirror = _adapter(), _adapter(OTHER, "reg-mirror")
    lookup = FakeLookup(central, mirror)
    result = _dependencies(JACKSON, "2.17.2", central, lookup)
    assert lookup.asked[0].name == "jackson-base"                       # the lookup is asked for the parent's group
    assert OTHER + parent in upstream.urls
    assert {d.constraint for d in result.dependencies if d.target.name == "jackson-core"} == {"2.17.2"}
    paths = {d.path for d in result.documents}
    assert parent not in paths                                          # its own registry stores it when asked
    # The rest is found in Central first (jackson-bom, imported by jackson-base, is not asked of the mirror).
    assert paths == {JACKSON_POM, *JACKSON_PARENTS[1:]}
    assert OTHER + JACKSON_PARENTS[1] not in upstream.urls


def test_another_repositorys_poms_are_cached_under_its_own_base_url(serve) -> None:
    parent = JACKSON_PARENTS[0]
    upstream = serve({BASE + parent: httpx.Response(404)}, hosts=("repo1.maven.org", "mirror.example.org"))
    central, mirror = _adapter(), _adapter(OTHER, "reg-mirror")
    lookup = FakeLookup(central, mirror)
    _dependencies(JACKSON, "2.17.2", central, lookup)
    upstream.requests.clear()
    _dependencies(JACKSON, "2.17.2", central, lookup)
    assert upstream.urls == [BASE + JACKSON_POM, BASE + parent]         # the miss here is asked again; the mirror is not


def test_the_lookup_is_optional_and_a_parent_found_nowhere_is_a_problem(serve) -> None:
    serve({BASE + JACKSON_PARENTS[0]: httpx.Response(404),
           _metadata_url(JACKSON): httpx.Response(200, text=_metadata(["2.17.2"], release="2.17.2"))})
    result = _dependencies(JACKSON, "2.17.2")
    assert result.available and [d.path for d in result.documents] == [JACKSON_POM]
    package = _fetch(JACKSON)
    assert package is not None
    problems = _version(package, "2.17.2").raw_metadata["problems"]
    assert isinstance(problems, list) and problems[0].startswith("parent com.fasterxml.jackson:jackson-base")


def test_a_parent_with_an_unresolved_version_is_not_asked_for(serve) -> None:
    pom = _xml("<parent><groupId>g</groupId><artifactId>p</artifactId><version>${revision}</version></parent>"
               "<artifactId>a</artifactId><dependencies><dependency><groupId>x</groupId><artifactId>y</artifactId>"
               "<version>1</version></dependency></dependencies>")
    upstream = serve({_pom_url("g:a", "1.0"): httpx.Response(200, text=pom)})
    result = _dependencies("g:a", "1.0")
    assert result.available and [d.target.name for d in result.dependencies] == ["y"]
    assert upstream.urls == [_pom_url("g:a", "1.0")]


def test_parent_errors_are_source_errors(serve) -> None:
    serve({BASE + JACKSON_PARENTS[0]: httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        _dependencies(JACKSON, "2.17.2")


def test_the_effective_model_of_a_latest_with_a_parent_goes_into_the_package(serve) -> None:
    serve({_metadata_url(JACKSON): httpx.Response(200, text=_metadata(["2.17.2"], release="2.17.2"))})
    package = _fetch(JACKSON)
    assert package is not None
    latest = _version(package, "2.17.2")
    assert latest.dependencies and {d.target.name for d in latest.dependencies} >= {"jackson-core", "jackson-annotations"}
    assert latest.raw_metadata["packaging"] == "jar"
    managed = latest.raw_metadata["managed"]
    assert isinstance(managed, list) and {"group": "com.fasterxml.jackson.core", "artifact": "jackson-core",
                                          "version": "2.17.2"} in managed


# --- dependency mapping -----------------------------------------------------------------------------------------


def test_scopes_become_dependency_types_and_raw_specs() -> None:
    model = _model("""<dependencies>
      <dependency><groupId>x</groupId><artifactId>none</artifactId><version>1</version></dependency>
      <dependency><groupId>x</groupId><artifactId>compile</artifactId><version>1</version><scope>compile</scope></dependency>
      <dependency><groupId>x</groupId><artifactId>runtime</artifactId><version>2</version><scope>runtime</scope></dependency>
      <dependency><groupId>x</groupId><artifactId>test</artifactId><version>3</version><scope>test</scope></dependency>
      <dependency><groupId>x</groupId><artifactId>provided</artifactId><version>4</version><scope>provided</scope></dependency>
      <dependency><groupId>x</groupId><artifactId>system</artifactId><version>5</version><scope>system</scope></dependency>
      <dependency><groupId>x</groupId><artifactId>optional</artifactId><version>6</version><scope>test</scope><optional>true</optional></dependency>
      <dependency><groupId>x</groupId><artifactId>unversioned</artifactId></dependency>
      <dependency><groupId>x</groupId><artifactId>range</artifactId><version>[1.0,2.0)</version></dependency>
    </dependencies>""")
    result = {d.target.name: (d.dependency_type, d.scope, d.constraint, d.raw_spec) for d in dependencies_of(model)}
    assert result == {
        "none": ("runtime", "compile", "1", "x:none:1"),
        "compile": ("runtime", "compile", "1", "x:compile:1"),
        "runtime": ("runtime", "runtime", "2", "x:runtime:2:runtime"),
        "test": ("dev", "test", "3", "x:test:3:test"),
        "provided": ("provided", "provided", "4", "x:provided:4:provided"),
        "system": ("provided", "system", "5", "x:system:5:system"),
        "optional": ("optional", "test", "6", "x:optional:6:test"),
        "unversioned": ("runtime", "compile", "", "x:unversioned:"),
        "range": ("runtime", "compile", "[1.0,2.0)", "x:range:[1.0,2.0)"),
    }


def test_dependency_targets_are_maven_package_refs() -> None:
    model = _model("<dependencies><dependency><groupId>org.x</groupId><artifactId>lib</artifactId><version>1</version></dependency></dependencies>")
    assert [d.target for d in dependencies_of(model)] == [PackageRef("maven", "org.x", "lib")]


def test_details_hold_only_what_applies() -> None:
    model = _model("""<dependencies>
      <dependency><groupId>x</groupId><artifactId>plain</artifactId><version>1</version></dependency>
      <dependency><groupId>x</groupId><artifactId>excl</artifactId><version>1</version>
        <exclusions><exclusion><groupId>a</groupId><artifactId>b</artifactId></exclusion>
                    <exclusion><groupId>c</groupId><artifactId>*</artifactId></exclusion></exclusions></dependency>
      <dependency><groupId>x</groupId><artifactId>typed</artifactId><version>1</version><type>test-jar</type>
        <classifier>tests</classifier></dependency>
      <dependency><groupId>x</groupId><artifactId>env</artifactId><version>${env.V}</version></dependency>
    </dependencies>""")
    result = {d.target.name: d.details for d in dependencies_of(model)}
    assert result["plain"] is None
    assert result["excl"] == {"exclusions": [{"group": "a", "artifact": "b"}, {"group": "c", "artifact": "*"}]}
    assert result["typed"] == {"type": "test-jar", "classifier": "tests"}
    assert result["env"] == {"unresolved_property": ["env.V"]}


def test_a_dependency_whose_coordinates_are_not_coordinates_is_skipped(caplog) -> None:
    model = _model("""<dependencies>
      <dependency><groupId>${undefined.group}</groupId><artifactId>x</artifactId><version>1</version></dependency>
      <dependency><groupId>x</groupId><artifactId>${undefined}</artifactId><version>1</version></dependency>
      <dependency><groupId>x y</groupId><artifactId>bad</artifactId><version>1</version></dependency>
      <dependency><groupId>ok</groupId><artifactId>ok</artifactId><version>1</version></dependency>
    </dependencies>""")
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        result = dependencies_of(model)
    assert [d.target.namespace for d in result] == ["ok"] and caplog.text.count("Skipping Maven dependency") == 3


def test_versions_come_from_dependency_management_and_stay_ranges() -> None:
    model = _model("""<dependencyManagement><dependencies>
        <dependency><groupId>x</groupId><artifactId>managed</artifactId><version>9</version><scope>runtime</scope></dependency>
      </dependencies></dependencyManagement>
      <dependencies><dependency><groupId>x</groupId><artifactId>managed</artifactId></dependency></dependencies>""")
    [dep] = dependencies_of(model)
    assert (dep.constraint, dep.scope, dep.dependency_type, dep.raw_spec) == ("9", "runtime", "runtime", "x:managed:9:runtime")


# --- relocation, Gradle marker, managed ------------------------------------------------------------------------


def test_a_relocation_is_a_deprecation(serve) -> None:
    version = "8.0.33"
    serve({_metadata_url("mysql:mysql-connector-java"): httpx.Response(200, text=_metadata([version], release=version))})
    package = _fetch("mysql:mysql-connector-java")
    assert package is not None
    assert package.deprecation == {
        "message": "MySQL Connector/J artifacts moved to reverse-DNS compliant Maven 2+ coordinates.",
        "alternative": "com.mysql:mysql-connector-j"}
    latest = _version(package, version)
    assert latest.dependencies == [] and latest.raw_metadata == {"packaging": "jar", "managed": []}   # no marker
    assert [a.has_metadata_file for a in latest.artifacts] == [False, False]


@pytest.mark.parametrize(("relocation", "expected"), [
    ("<message>moved</message><groupId>g2</groupId>", {"message": "moved", "alternative": "g2:a"}),
    ("<artifactId>a2</artifactId>", {"message": "relocated", "alternative": "g:a2"}),
    ("<groupId>g2</groupId><artifactId>a2</artifactId><version>3.0</version>", {"message": "relocated", "alternative": "g2:a2:3.0"}),
    ("", {"message": "relocated", "alternative": "g:a"}),
])
def test_relocation_parts_missing_are_taken_from_the_artifact_itself(serve, relocation: str, expected) -> None:
    pom = _xml(f"<groupId>g</groupId><artifactId>a</artifactId><version>1</version>"
               f"<distributionManagement><relocation>{relocation}</relocation></distributionManagement>")
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1"], release="1")),
           _pom_url("g:a", "1"): httpx.Response(200, text=pom)})
    package = _fetch("g:a")
    assert package is not None and package.deprecation == expected


def test_the_gradle_marker_sets_has_metadata_file_and_raw_metadata(serve) -> None:
    version = "1.9.0"
    serve({_metadata_url("org.jetbrains.kotlinx:kotlinx-coroutines-core-jvm"): httpx.Response(
        200, text=_metadata([version], release=version))})
    package = _fetch("org.jetbrains.kotlinx:kotlinx-coroutines-core-jvm")
    assert package is not None
    latest = _version(package, version)
    assert latest.raw_metadata["gradle_metadata"] is True
    assert [(a.artifact_type, a.has_metadata_file) for a in latest.artifacts] == [("pom", True), ("jar", False)]
    assert latest.dependencies   # kotlinx-coroutines-core-jvm depends on its siblings and annotations


def test_managed_entries_omit_defaults_and_the_problems_are_kept(serve) -> None:
    pom = _xml("""<groupId>g</groupId><artifactId>a</artifactId><version>1</version><packaging>pom</packaging>
      <parent><groupId>g</groupId><artifactId>gone</artifactId><version>1</version></parent>
      <dependencyManagement><dependencies>
        <dependency><groupId>x</groupId><artifactId>plain</artifactId><version>1</version></dependency>
        <dependency><groupId>x</groupId><artifactId>full</artifactId><version>2</version><scope>test</scope>
          <type>test-jar</type><classifier>tests</classifier></dependency>
        <dependency><groupId>x</groupId><artifactId>bom</artifactId><version>3</version><type>pom</type><scope>import</scope></dependency>
      </dependencies></dependencyManagement>""")
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1"], release="1")),
           _pom_url("g:a", "1"): httpx.Response(200, text=pom)})
    package = _fetch("g:a")
    assert package is not None
    latest = _version(package, "1")
    assert latest.raw_metadata["packaging"] == "pom"
    assert latest.raw_metadata["managed"] == [
        {"group": "x", "artifact": "plain", "version": "1"},
        {"group": "x", "artifact": "full", "version": "2", "scope": "test", "type": "test-jar", "classifier": "tests"}]
    assert latest.raw_metadata["problems"] == ["parent g:gone:1 not found", "BOM x:bom:3 not found"]
    assert [a.artifact_type for a in latest.artifacts] == ["pom"]        # a pom-packaged artifact has no main file


@pytest.mark.parametrize(("packaging", "extension"), [
    ("jar", "jar"), ("bundle", "jar"), ("maven-plugin", "jar"), ("eclipse-plugin", "jar"), ("takari-jar", "jar"),
    ("hk2-jar", "jar"), ("war", "war"), ("aar", "aar"), ("ear", "ear"), ("nbm", "nbm")])
def test_the_main_file_of_the_latest_follows_its_packaging(serve, packaging: str, extension: str) -> None:
    pom = _xml(f"<groupId>g</groupId><artifactId>a</artifactId><version>1</version><packaging>{packaging}</packaging>")
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1"], release="1")),
           _pom_url("g:a", "1"): httpx.Response(200, text=pom)})
    package = _fetch("g:a")
    assert package is not None
    assert [(a.filename, a.artifact_type) for a in _version(package, "1").artifacts] == [
        ("a-1.pom", "pom"), (f"a-1.{extension}", extension)]


def test_developers_without_a_name_are_skipped_and_none_when_the_pom_lists_none(serve) -> None:
    def served(developers: str) -> None:
        pom = _xml(f"<groupId>g</groupId><artifactId>a</artifactId><version>1</version>{developers}")
        serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1"], release="1")),
               _pom_url("g:a", "1"): httpx.Response(200, text=pom)})

    served("<developers><developer><id>x</id></developer><developer><name>Ann</name></developer></developers>")
    package = _fetch("g:a")
    assert package is not None and package.authors == [NormalizedAuthor("Ann", None, None, "author")]
    served("<developers><developer><id>x</id></developer></developers>")
    package = _fetch("g:a")
    assert package is not None and package.authors is None


# --- fetch_version_dependencies ---------------------------------------------------------------------------------


def test_version_dependencies_of_a_version_pom(serve) -> None:
    serve()
    result = _dependencies(SPRING, "6.1.14")
    assert result.available and [(d.target.name, d.constraint) for d in result.dependencies] == [("spring-jcl", "6.1.14")]
    assert result.documents == [FetchedDocument(
        path="org/springframework/spring-core/6.1.14/spring-core-6.1.14.pom",
        content=_recorded("org/springframework/spring-core/6.1.14/spring-core-6.1.14.pom"),
        content_type="application/xml", etag=HEADERS["org/springframework/spring-core/6.1.14/spring-core-6.1.14.pom"]["etag"])]


def test_version_dependencies_of_a_missing_pom_are_unavailable(serve) -> None:
    serve()
    result = _dependencies(SPRING, "0.0.1")
    assert result.available is False and result.dependencies == [] and list(result.documents) == []


def test_version_dependencies_of_an_unreadable_pom_are_unavailable(serve, caplog) -> None:
    serve({_pom_url("g:a", "1"): httpx.Response(200, text="not xml")})
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        result = _dependencies("g:a", "1")
    assert result.available is False and result.dependencies == [] and "Unreadable POM of g:a@1" in caplog.text


def test_version_dependencies_raise_on_a_server_error(serve) -> None:
    serve({_pom_url("g:a", "1"): httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        _dependencies("g:a", "1")


# --- fetch_document, exists, test_connection --------------------------------------------------------------------


def _document(path: str, adapter: Maven2Adapter | None = None) -> FetchedDocument | None:
    return asyncio.run((adapter or _adapter()).fetch_document(path))


def test_fetch_document(serve) -> None:
    pom = "com/google/guava/guava-parent/33.3.1-jre/guava-parent-33.3.1-jre.pom"
    upstream = serve()
    document = _document(pom)
    assert document == FetchedDocument(path=pom, content=_recorded(pom), content_type="text/xml", etag=HEADERS[pom]["etag"])
    assert upstream.urls == [BASE + pom]


def test_fetch_document_without_etag_and_with_a_leading_slash(serve) -> None:
    serve({BASE + "g/a/1/a-1.pom": httpx.Response(200, text="<project/>")})
    document = _document("/g/a/1/a-1.pom")
    assert document is not None and document.etag is None and document.content == b"<project/>"
    assert document.path == "/g/a/1/a-1.pom"


@pytest.mark.parametrize("status", [404, 410])
def test_fetch_document_of_something_missing_is_none(serve, status: int) -> None:
    serve({BASE + "g/a/1/a-1.pom": httpx.Response(status)})
    assert _document("g/a/1/a-1.pom") is None


def test_fetch_document_raises_on_a_server_error(serve) -> None:
    serve({BASE + "g/a/1/a-1.pom": httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        _document("g/a/1/a-1.pom")


def test_the_base_url_may_come_without_a_trailing_slash(serve) -> None:
    upstream = serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata([]))})
    assert _fetch("g:a", _adapter("https://repo1.maven.org/maven2")) is not None
    assert upstream.urls == [_metadata_url("g:a")]


def test_exists(serve) -> None:
    upstream = serve()
    adapter = _adapter()
    assert asyncio.run(adapter.exists(_ref(SPRING))) is True
    assert upstream.urls == [BASE + SPRING_METADATA]
    assert asyncio.run(adapter.exists(_ref("org.example:nothing"))) is False


def test_exists_is_false_for_410_and_raises_on_a_server_error(serve) -> None:
    serve({_metadata_url("g:gone"): httpx.Response(410), _metadata_url("g:down"): httpx.Response(500)})
    assert asyncio.run(_adapter().exists(_ref("g:gone"))) is False
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().exists(_ref("g:down")))


MAVEN_ARTIFACT = BASE + "org/apache/maven/maven-artifact/maven-metadata.xml"


@pytest.mark.parametrize("status", [200, 404])
def test_test_connection_accepts_a_reachable_repository(serve, status: int) -> None:
    upstream = serve({MAVEN_ARTIFACT: httpx.Response(status, text="<metadata/>")})
    asyncio.run(_adapter().test_connection())
    assert upstream.urls == [MAVEN_ARTIFACT]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_test_connection_fails_on_any_other_status(serve, status: int) -> None:
    serve({MAVEN_ARTIFACT: httpx.Response(status)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())


def test_test_connection_retries_a_server_error_then_raises(serve) -> None:
    upstream = serve({MAVEN_ARTIFACT: httpx.Response(500)})
    adapter = _adapter()
    adapter.max_retries = 2
    adapter.retry_base_seconds = adapter.retry_max_seconds = 0.001
    adapter.retry_jitter_seconds = 0.0
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.test_connection())
    assert len(upstream.requests) == 3


def test_a_transient_error_is_retried_and_then_succeeds(monkeypatch) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, text=_metadata(["1"]))

    monkeypatch.setattr(maven2, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(flaky)))
    adapter = _adapter()
    adapter.max_retries = 2
    adapter.retry_base_seconds = adapter.retry_max_seconds = 0.001
    adapter.retry_jitter_seconds = 0.0
    assert asyncio.run(adapter.exists(_ref("g:a"))) is True and calls == 2


def test_min_interval_is_zero() -> None:
    assert _adapter().min_interval_seconds() == 0.0


# --- the change detector ---------------------------------------------------------------------------------------


def _tracked(raw: str, revision: str | None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(ref=_ref(raw), package_id=raw, revision=revision, etag=None, unavailable=unavailable)


def _detect(tracked: list[TrackedPackage], adapter: Maven2Adapter | None = None):
    return asyncio.run((adapter or _adapter()).change_detector().detect(tracked, {"anything": 1}))


def test_the_detector_is_the_maven2_one() -> None:
    assert isinstance(_adapter().change_detector(), Maven2ChangeDetector)


def test_an_etag_revision_is_checked_with_a_conditional_get(serve) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"same"':
            return httpx.Response(304)
        return httpx.Response(200, text=_metadata(["1"]), headers={"etag": '"new"'})

    upstream = serve({_metadata_url("g:same"): route, _metadata_url("g:grown"): route})
    result = _detect([_tracked("g:same", '"same"'), _tracked("g:grown", '"old"')])
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("grown", '"new"')]
    assert result.missing == [] and result.cursor == {} and not result.reconcile_all
    assert sorted(r.headers["if-none-match"] for r in upstream.requests) == ['"old"', '"same"']
    assert all(request.method == "GET" for request in upstream.requests)


def test_a_weak_etag_is_conditional_too(serve) -> None:
    upstream = serve({_metadata_url("g:a"): httpx.Response(304)})
    assert _detect([_tracked("g:a", 'W/"abc"')]).changed == []
    assert upstream.requests[0].headers["if-none-match"] == 'W/"abc"'


def test_a_200_with_the_stored_etag_is_no_change(serve) -> None:
    # A repository that ignores If-None-Match answers 200 with the same ETag.
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1"]), headers={"etag": '"same"'})})
    assert _detect([_tracked("g:a", '"same"')]).changed == []


def test_unavailable_with_a_304_is_changed_with_the_same_revision(serve) -> None:
    serve({_metadata_url("g:a"): httpx.Response(304)})
    result = _detect([_tracked("g:a", '"same"', unavailable=True)])
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("a", '"same"')]


def test_missing_and_already_missing(serve) -> None:
    serve({_metadata_url("g:gone"): httpx.Response(404), _metadata_url("g:gone2"): httpx.Response(410),
           _metadata_url("g:already"): httpx.Response(404)})
    result = _detect([_tracked("g:gone", '"x"'), _tracked("g:gone2", "lastUpdated:1"),
                      _tracked("g:already", '"x"', unavailable=True)])
    assert [p.ref.name for p in result.missing] == ["gone", "gone2"] and result.changed == []


def test_a_package_never_synced_is_changed_without_a_request(serve) -> None:
    upstream = serve()
    result = _detect([_tracked("g:a", None)])
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("a", None)] and upstream.requests == []


def test_a_revision_that_is_not_an_etag_is_checked_with_a_full_get(serve) -> None:
    upstream = serve({
        _metadata_url("g:same"): httpx.Response(200, text=_metadata(["1"], last_updated="20260101000000")),
        _metadata_url("g:newer"): httpx.Response(200, text=_metadata(["1", "2"], last_updated="20260202000000")),
        _metadata_url("g:hash"): httpx.Response(200, text=_metadata(["2", "1"])),
        _metadata_url("g:hash-new"): httpx.Response(200, text=_metadata(["1", "2", "3"])),
        _metadata_url("g:back"): httpx.Response(200, text=_metadata(["1"], last_updated="20260101000000")),
    })
    result = _detect([
        _tracked("g:same", "lastUpdated:20260101000000"), _tracked("g:newer", "lastUpdated:20260101000000"),
        _tracked("g:hash", hashlib.sha256(b"1\n2").hexdigest()), _tracked("g:hash-new", hashlib.sha256(b"1\n2").hexdigest()),
        _tracked("g:back", "lastUpdated:20260101000000", unavailable=True),
    ])
    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("newer", "lastUpdated:20260202000000"), ("hash-new", hashlib.sha256(b"1\n2\n3").hexdigest()),
        ("back", "lastUpdated:20260101000000")]
    assert all("if-none-match" not in request.headers for request in upstream.requests)


def test_snapshots_do_not_change_a_hash_revision(serve) -> None:
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1", "2-SNAPSHOT", "bad version"]))})
    assert _detect([_tracked("g:a", hashlib.sha256(b"1").hexdigest())]).changed == []


def test_the_detector_agrees_with_the_revision_fetch_package_stores(serve) -> None:
    serve()
    package = _fetch(SPRING)
    assert package is not None
    assert _detect([_tracked(SPRING, package.revision)]).changed == []
    serve({_metadata_url("g:a"): httpx.Response(200, text=_metadata(["1"], release="1", last_updated="20260101000000"))})
    stored = _fetch("g:a")
    assert stored is not None
    assert _detect([_tracked("g:a", stored.revision)]).changed == []


def test_the_detector_survives_one_failing_package(serve, caplog) -> None:
    serve({
        _metadata_url("g:ok"): httpx.Response(200, text=_metadata(["1"]), headers={"etag": '"new"'}),
        _metadata_url("g:broken"): httpx.Response(503),
        _metadata_url("g:garbled"): httpx.Response(200, text="<html>"),
    })
    with caplog.at_level(logging.WARNING, logger=maven2.logger.name):
        result = _detect([_tracked("g:ok", '"old"'), _tracked("g:broken", '"old"'),
                          _tracked("g:garbled", "lastUpdated:1")])
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("ok", '"new"')]
    assert result.missing == [] and "g:broken" in caplog.text and "g:garbled" in caplog.text


def test_the_detector_bounds_the_concurrency(monkeypatch) -> None:
    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(304)

    monkeypatch.setattr(maven2, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    tracked = [_tracked(f"g:a{i}", '"x"') for i in range(8)]
    result = asyncio.run(Maven2ChangeDetector(_adapter(), concurrency=3).detect(tracked, {}))
    assert result.changed == [] and result.missing == [] and peak == 3
