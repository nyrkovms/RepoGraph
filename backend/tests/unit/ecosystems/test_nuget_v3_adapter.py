"""The `nuget-v3` adapter (WP I3) on real answers of nuget.org (tests/fixtures/nuget/), recorded on 2026-10-30 with
`curl -A "PackageHub/0.1 (+https://github.com/nyrkovms/RepoGraph)" --compressed`:

- service-index.json (api.nuget.org/v3/index.json): as recorded;
- registration-newtonsoft-json.json (registration5-gz-semver2/newtonsoft.json/index.json, pages inline): unlisted
  versions (dated 1900-01-01), dependency groups of many frameworks, empty groups; trimmed to 10 of 86 versions;
- registration-microsoft.extensions.logging.json and page-microsoft.extensions.logging-*.json (the same under
  microsoft.extensions.logging/, pages external: the index lists three pages, each its own file); every page trimmed
  to 3 of 64/64/56 versions;
- registration-microsoft.azure.keyvault.json (microsoft.azure.keyvault/index.json): deprecated versions with an
  alternate package, one unlisted version, a version whose dependencies have no group; trimmed to 6 of 24.
Trimming only dropped whole versions (the `count`, `lower` and `upper` of the pages are as recorded). The ETags below
are those of the full answers. Everything is served by a fake upstream; nothing here reaches the network. nuget.org
returned no `exclude`/`include` on dependencies in any of the dozen packages looked at, so those and the edge cases of
the document are tested on small documents built here."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.ecosystems.base import InvalidVersion, NormalizedPackage, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.nuget.adapters import NuGetEtagChangeDetector, NuGetV3Adapter
from app.ecosystems.nuget.adapters import v3
from app.ecosystems.nuget.adapters.v3 import ServiceIndex, parse_service_index
from app.ecosystems.nuget.changes import NuGetCatalogChangesDetector
from app.ecosystems.nuget.coordinates import NuGetCoordinates
from app.ecosystems.nuget.environment import dependency_applies, parse_environment

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "nuget"
INDEX = "https://api.nuget.org/v3/index.json"
REGISTRATIONS = "https://api.nuget.org/v3/registration5-gz-semver2/"
NEWTONSOFT = f"{REGISTRATIONS}newtonsoft.json/index.json"
NEWTONSOFT_ETAG = "0x8DF17C9E7600112"
LOGGING_ETAG = "0x8DF32C25ABC2138"
KEYVAULT_ETAG = "0x8DE969974FCC3A2"
NO_LOOKUP = None  # the adapter does not use the lookup
LOGGING_PAGES = {
    "microsoft.extensions.logging/page/1.0.0-rc1-final/3.1.23.json": "page-microsoft.extensions.logging-3.1.23.json",
    "microsoft.extensions.logging/page/3.1.24/9.0.0.json": "page-microsoft.extensions.logging-9.0.0.json",
    "microsoft.extensions.logging/page/9.0.1/11.0.0-rc.3.26522.148.json":
        "page-microsoft.extensions.logging-11.0.0-rc.3.26522.148.json",
}
NEWTONSOFT_GROUPS = [".NETFramework2.0", ".NETFramework3.5", ".NETFramework4.0", ".NETFramework4.5",
                     ".NETStandard1.0", ".NETStandard1.3", ".NETStandard2.0"]
DEMO = f"{REGISTRATIONS}demo.pkg/index.json"


def _registry(base_url: str = INDEX, **config: object) -> RegistryConfig:
    return RegistryConfig(
        id="reg-nuget", slug="nuget-org", ecosystem="nuget", protocol="nuget-v3", base_url=base_url,
        priority=0, config=config, routing=None, auth_type="none", credentials_ref=None,
    )


def _ref(raw: str) -> PackageRef:
    return NuGetCoordinates().parse(raw)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _ok(name: str, etag: str | None = None) -> httpx.Response:
    return httpx.Response(200, text=_text(name), headers={"etag": etag} if etag else {})


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
        upstream = Upstream({INDEX: _ok("service-index.json"), **routes})
        monkeypatch.setattr(
            v3, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
        )
        return upstream

    return start


def _all_fixtures() -> dict[str, httpx.Response]:
    routes = {
        NEWTONSOFT: _ok("registration-newtonsoft-json.json", NEWTONSOFT_ETAG),
        f"{REGISTRATIONS}microsoft.azure.keyvault/index.json":
            _ok("registration-microsoft.azure.keyvault.json", KEYVAULT_ETAG),
        f"{REGISTRATIONS}microsoft.extensions.logging/index.json":
            _ok("registration-microsoft.extensions.logging.json", LOGGING_ETAG),
    }
    for page, name in LOGGING_PAGES.items():
        routes[f"{REGISTRATIONS}{page}"] = _ok(name)
    return routes


def _adapter(registry: RegistryConfig | None = None, **config: object) -> NuGetV3Adapter:
    adapter = NuGetV3Adapter(registry or _registry(**config))
    adapter.max_retries = 0   # a 5xx in a test is an answer, not something to wait out
    return adapter


def _fetch(adapter: NuGetV3Adapter, raw: str) -> NormalizedPackage | None:
    return asyncio.run(adapter.fetch_package(_ref(raw), NO_LOOKUP))  # type: ignore[arg-type]


def _deps(adapter: NuGetV3Adapter, raw: str, version: str):
    return asyncio.run(adapter.fetch_version_dependencies(_ref(raw), version, NO_LOOKUP))  # type: ignore[arg-type]


def _version(package: NormalizedPackage, number: str):
    return next(v for v in package.versions if v.version == number)


def _document(*entries: dict) -> httpx.Response:
    """A registration index with one inline page; each entry is the `catalogEntry` of a version."""
    items = [{"packageContent": f"https://feed.example/{e['version']}.nupkg", "catalogEntry": e} for e in entries]
    return httpx.Response(200, json={"count": 1, "items": [{"@id": "p", "count": len(items), "items": items}]})


def _entry(version: str = "1.0.0", **fields: object) -> dict:
    return {"id": "Demo.Pkg", "version": version, "listed": True, "published": "2024-05-01T10:00:00+00:00", **fields}


def _group(framework: str | None, *dependencies: dict) -> dict:
    group: dict = {"dependencies": list(dependencies)} if dependencies else {}
    if framework is not None:
        group["targetFramework"] = framework
    return group


def _resources(*pairs: tuple[str, str]) -> dict:
    return {"version": "3.0.0", "resources": [{"@id": url, "@type": kind} for kind, url in pairs]}


# --- the service index ---------------------------------------------------------------------------------------------


def test_the_service_index_of_nuget_org_gives_the_semver2_registrations_and_the_flat_container() -> None:
    service = parse_service_index(json.loads(_text("service-index.json")), INDEX)
    assert service == ServiceIndex(registrations=REGISTRATIONS, flat_container="https://api.nuget.org/v3-flatcontainer/")


def test_the_registrations_resource_is_the_best_the_feed_offers() -> None:
    everything = _resources(
        ("RegistrationsBaseUrl", "https://f/plain"), ("RegistrationsBaseUrl/Versioned", "https://f/versioned"),
        ("RegistrationsBaseUrl/3.6.0", "https://f/semver2/"),
    )
    assert parse_service_index(everything, INDEX).registrations == "https://f/semver2/"
    versioned = _resources(("RegistrationsBaseUrl", "https://f/plain"), ("RegistrationsBaseUrl/Versioned", "https://f/versioned"))
    assert parse_service_index(versioned, INDEX).registrations == "https://f/versioned/"
    plain = _resources(("RegistrationsBaseUrl", "https://f/plain"), ("SearchQueryService", "https://f/search"))
    assert parse_service_index(plain, INDEX) == ServiceIndex("https://f/plain/", None)


def test_the_service_index_is_read_once_per_adapter(serve) -> None:
    upstream = serve(_all_fixtures())
    adapter = _adapter()
    assert _fetch(adapter, "newtonsoft.json") is not None
    assert _fetch(adapter, "microsoft.azure.keyvault") is not None
    assert asyncio.run(adapter.exists(_ref("newtonsoft.json"))) is True
    assert upstream.urls.count(INDEX) == 1


def test_a_registration_is_asked_for_under_the_lowercase_id(serve) -> None:
    upstream = serve(_all_fixtures())
    assert _fetch(_adapter(), "Newtonsoft.Json") is not None
    assert upstream.urls == [INDEX, NEWTONSOFT]


# --- the package ---------------------------------------------------------------------------------------------------


def test_newtonsoft_json_with_inline_pages(serve) -> None:
    upstream = serve(_all_fixtures())
    package = _fetch(_adapter(), "newtonsoft.json")

    assert package is not None
    assert upstream.urls == [INDEX, NEWTONSOFT]   # inline pages: nothing else to fetch
    assert package.ref == _ref("newtonsoft.json") and package.registry_id == "reg-nuget"
    assert package.display_name == "Newtonsoft.Json"
    assert package.revision == NEWTONSOFT_ETAG
    assert [v.version for v in package.versions] == [
        "3.5.8", "10.0.1-beta1", "10.0.1", "10.0.2", "12.0.1-beta2",
        "13.0.1", "13.0.2-beta1", "13.0.4", "14.0.1-beta1", "14.0.1-beta2",
    ]
    assert package.description == "Json.NET is a popular high-performance JSON framework for .NET"
    assert package.homepage_url == "https://www.newtonsoft.com/json"
    assert package.license_raw == "MIT" and package.license_spdx == "MIT"
    assert package.keywords == ["json"]
    assert [(a.name, a.role, a.email, a.url) for a in package.authors or []] == [("James Newton-King", "author", None, None)]
    assert package.deprecation is None
    assert package.vulnerabilities is None   # D6


def test_unlisted_versions_and_dates(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "newtonsoft.json")
    assert package is not None
    assert {v.version: v.status for v in package.versions} == {
        "3.5.8": "active", "10.0.1-beta1": "unlisted", "10.0.1": "active", "10.0.2": "active",
        "12.0.1-beta2": "unlisted", "13.0.1": "active", "13.0.2-beta1": "unlisted", "13.0.4": "active",
        "14.0.1-beta1": "active", "14.0.1-beta2": "active",
    }
    assert _version(package, "10.0.1-beta1").release_date is None   # published 1900-01-01
    released = _version(package, "13.0.4").release_date
    assert released is not None and released.year == 2025 and released.tzinfo is not None


def test_the_artifact_of_a_version_is_its_nupkg(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "newtonsoft.json")
    assert package is not None
    (artifact,) = _version(package, "13.0.1").artifacts
    assert artifact.filename == "newtonsoft.json.13.0.1.nupkg" and artifact.artifact_type == "nupkg"
    assert artifact.download_url == "https://api.nuget.org/v3-flatcontainer/newtonsoft.json/13.0.1/newtonsoft.json.13.0.1.nupkg"


def test_dependencies_come_with_every_version_per_group(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "newtonsoft.json")
    assert package is not None
    assert all(v.dependencies is not None for v in package.versions)   # none of them is lazy
    dependencies = _version(package, "13.0.1").dependencies or []
    # .NETStandard1.0 has four, .NETStandard1.3 six; the other five groups are empty
    assert len(dependencies) == 4 + 6
    standard10 = [d for d in dependencies if d.environment_markers == {"dotnet_target_framework": ".NETStandard1.0"}]
    assert [(d.target.name, d.constraint, d.raw_spec) for d in standard10] == [
        ("microsoft.csharp", "[4.3.0, )", "Microsoft.CSharp [4.3.0, )"),
        ("netstandard.library", "[1.6.1, )", "NETStandard.Library [1.6.1, )"),
        ("system.componentmodel.typeconverter", "[4.3.0, )", "System.ComponentModel.TypeConverter [4.3.0, )"),
        ("system.runtime.serialization.primitives", "[4.3.0, )", "System.Runtime.Serialization.Primitives [4.3.0, )"),
    ]
    assert all(d.dependency_type == "runtime" and d.scope == "dependency" and d.alias is None for d in dependencies)
    assert all(d.details == {"groups": NEWTONSOFT_GROUPS} for d in dependencies)


def test_a_group_without_dependencies_counts_only_in_groups(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "newtonsoft.json")
    assert package is not None
    assert _version(package, "14.0.1-beta2").dependencies == []   # eight groups, none with dependencies
    assert _version(package, "3.5.8").dependencies == []   # no groups at all


def test_the_groups_detail_lets_the_environment_pick_one_group(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "newtonsoft.json")
    assert package is not None
    dependencies = _version(package, "13.0.1").dependencies or []
    # net8.0 takes .NETStandard2.0, which is empty: the dependencies of the other groups do not apply (§9.6)
    assert not any(dependency_applies(d, parse_environment({"target_framework": "net8.0"}), False) for d in dependencies)
    taken = [d for d in dependencies if dependency_applies(d, parse_environment({"target_framework": "netstandard1.3"}), False)]
    assert len(taken) == 6
    assert {(d.environment_markers or {})["dotnet_target_framework"] for d in taken} == {".NETStandard1.3"}


def test_a_dependency_group_without_a_framework(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "microsoft.azure.keyvault")
    assert package is not None
    (dep,) = _version(package, "0.9.0-preview").dependencies or []
    assert dep.target.name == "microsoft.azure.common" and dep.constraint == "[2.0.4, 3.0.0)"
    assert dep.environment_markers == {"dotnet_target_framework": ""}
    assert dep.details == {"groups": [""]}
    assert dependency_applies(dep, parse_environment({"target_framework": "net8.0"}), False)


def test_external_pages_are_fetched(serve) -> None:
    upstream = serve(_all_fixtures())
    package = _fetch(_adapter(), "microsoft.extensions.logging")

    assert package is not None
    assert sorted(upstream.urls) == sorted([
        INDEX, f"{REGISTRATIONS}microsoft.extensions.logging/index.json", *(f"{REGISTRATIONS}{page}" for page in LOGGING_PAGES),
    ])
    assert package.display_name == "Microsoft.Extensions.Logging" and package.revision == LOGGING_ETAG
    assert [v.version for v in package.versions] == [
        "1.0.0-rc1-final", "2.1.0-rc1-final", "3.1.23", "3.1.24", "8.0.0-preview.6.23329.7", "9.0.0",
        "9.0.1", "10.0.1", "11.0.0-rc.3.26522.148",
    ]
    # the fields of the newest listed version, the 11.0.0 release candidate
    assert package.license_spdx == "MIT" and package.authors is not None and package.authors[0].name == "Microsoft"
    newest = _version(package, "11.0.0-rc.3.26522.148").dependencies or []
    assert [(d.environment_markers or {})["dotnet_target_framework"] for d in newest[:4]] == [
        "net10.0", "net10.0", "net10.0", "net11.0"]
    assert newest[0].details == {"groups": ["net10.0", "net11.0", ".NETFramework4.6.2", ".NETStandard2.0", ".NETStandard2.1"]}


def test_a_page_that_cannot_be_fetched_fails_the_package(serve) -> None:
    routes = _all_fixtures()
    del routes[f"{REGISTRATIONS}microsoft.extensions.logging/page/3.1.24/9.0.0.json"]   # the fake answers 404 for it
    serve(routes)
    with pytest.raises(httpx.HTTPStatusError):
        _fetch(_adapter(), "microsoft.extensions.logging")


def test_deprecated_versions(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "microsoft.azure.keyvault")
    assert package is not None
    deprecation = _version(package, "3.0.5").deprecation
    assert deprecation is not None
    assert deprecation["reasons"] == ["Legacy"]
    assert str(deprecation["message"]).startswith("Please note, this package is obsolete as of 3/31/2023")
    assert deprecation["alternate"] == {"id": "Azure.Security.KeyVault.Certificates", "range": "*"}
    assert set(deprecation) == {"reasons", "message", "alternate"}   # the @id and @type of the document stay out
    assert all(v.deprecation is not None for v in package.versions)
    # the newest version is deprecated, so the package is
    assert package.deprecation == {"message": deprecation["message"]}
    assert package.display_name == "Microsoft.Azure.KeyVault"
    assert package.revision == KEYVAULT_ETAG


def test_an_unlisted_deprecated_version(serve) -> None:
    serve(_all_fixtures())
    package = _fetch(_adapter(), "microsoft.azure.keyvault")
    assert package is not None
    version = _version(package, "2.3.1")
    assert version.status == "unlisted" and version.deprecation is not None and version.release_date is None
    assert _version(package, "2.3.2").status == "active"


def test_the_stored_documents_hold_no_json_nulls(serve) -> None:
    serve(_all_fixtures())
    for name in ("microsoft.azure.keyvault", "microsoft.extensions.logging", "newtonsoft.json"):
        package = _fetch(_adapter(), name)
        assert package is not None
        for version in package.versions:
            assert version.deprecation is None or None not in version.deprecation.values()
            for dep in version.dependencies or []:
                assert dep.environment_markers is not None and dep.details is not None
                assert isinstance(dep.environment_markers["dotnet_target_framework"], str)
                assert all(isinstance(g, str) for g in dep.details["groups"])  # type: ignore[attr-defined]


def test_a_missing_package_is_none_and_does_not_exist(serve) -> None:
    serve(_all_fixtures())
    adapter = _adapter()
    assert _fetch(adapter, "no.such.package") is None
    assert asyncio.run(adapter.exists(_ref("no.such.package"))) is False
    assert asyncio.run(adapter.exists(_ref("newtonsoft.json"))) is True


def test_a_server_error_is_raised(serve) -> None:
    serve({NEWTONSOFT: httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        _fetch(_adapter(), "newtonsoft.json")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().exists(_ref("newtonsoft.json")))


def test_without_an_etag_the_revision_is_a_hash_of_the_index(serve) -> None:
    response = _document(_entry())
    serve({DEMO: response})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.revision == hashlib.sha256(response.content).hexdigest()


# --- package fields ------------------------------------------------------------------------------------------------


def test_the_package_fields_come_from_the_newest_listed_version(serve) -> None:
    serve({DEMO: _document(
        _entry("1.0.0", description="old", projectUrl="https://old.example", authors="Old"),
        _entry("2.0.0", id="DEMO.pkg", description="new", projectUrl="https://new.example", authors="New"),
        _entry("3.0.0", id="Demo.Unlisted", listed=False, description="unlisted", authors="Nobody"),
    )})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.display_name == "DEMO.pkg" and package.description == "new"
    assert package.homepage_url == "https://new.example" and [a.name for a in package.authors or []] == ["New"]
    assert [(v.version, v.status) for v in package.versions] == [
        ("1.0.0", "active"), ("2.0.0", "active"), ("3.0.0", "unlisted")]


def test_with_nothing_listed_the_newest_version_gives_the_fields(serve) -> None:
    serve({DEMO: _document(
        _entry("2.0.0", id="Demo.New", listed=False, description="new"),
        _entry("1.0.0", id="Demo.Old", listed=False, description="old"),
    )})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None and package.display_name == "Demo.New" and package.description == "new"
    assert all(v.status == "unlisted" for v in package.versions)


def test_description_falls_back_to_the_summary_and_license_to_the_url(serve) -> None:
    serve({DEMO: _document(_entry(
        description="", summary="Short", licenseUrl="https://license.example/mit", tags=["", " a ", "b"],
        authors="Ann, Bob ,, Cy", projectUrl="",
    ))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.description == "Short"
    assert package.license_raw == "https://license.example/mit" and package.license_spdx is None
    assert package.keywords == ["a", "b"] and package.homepage_url is None
    assert [(a.name, a.role) for a in package.authors or []] == [("Ann", "author"), ("Bob", "author"), ("Cy", "author")]


def test_a_license_expression_wins_over_the_url(serve) -> None:
    serve({DEMO: _document(_entry(licenseExpression="MIT OR Apache-2.0", licenseUrl="https://aka.ms/deprecateLicenseUrl"))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.license_raw == "MIT OR Apache-2.0" and package.license_spdx == "MIT OR Apache-2.0"


def test_empty_fields_are_none(serve) -> None:
    serve({DEMO: _document(_entry(tags=[""], authors="", description=None))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.keywords is None and package.authors is None and package.description is None
    assert package.license_raw is None and package.deprecation is None


def test_a_deprecation_with_reasons_only(serve) -> None:
    serve({DEMO: _document(_entry(deprecation={"reasons": ["Legacy", "Other"]}))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.versions[0].deprecation == {"reasons": ["Legacy", "Other"]}
    assert package.deprecation == {"message": "Legacy, Other"}


def test_a_deprecated_older_version_does_not_deprecate_the_package(serve) -> None:
    serve({DEMO: _document(_entry("1.0.0", deprecation={"reasons": ["Legacy"], "message": "old"}), _entry("2.0.0"))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert package.deprecation is None
    assert package.versions[0].deprecation == {"reasons": ["Legacy"], "message": "old"} and package.versions[1].deprecation is None


def test_a_version_the_scheme_rejects_is_skipped_and_logged(serve, caplog) -> None:
    serve({DEMO: _document(_entry("1.0.0"), _entry("not-a-version"), _entry("2.0"), _entry("1.0.0.1"))})
    with caplog.at_level(logging.WARNING):
        package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert [v.version for v in package.versions] == ["1.0.0", "2.0.0", "1.0.0.1"]   # normalized by the scheme
    assert "'not-a-version'" in caplog.text
    (artifact,) = _version(package, "2.0.0").artifacts
    assert artifact.filename == "demo.pkg.2.0.0.nupkg"


def test_only_rejected_versions_leave_an_empty_package(serve) -> None:
    serve({DEMO: _document(_entry("nope"))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None and package.versions == [] and package.display_name == "demo.pkg"
    assert package.description is None


def test_the_download_url_is_the_package_content_else_the_flat_container(serve) -> None:
    items = [
        {"packageContent": "https://cdn.example/demo.pkg.1.0.0.nupkg", "catalogEntry": _entry("1.0.0")},
        {"catalogEntry": _entry("2.0.0")},
    ]
    serve({DEMO: httpx.Response(200, json={"count": 1, "items": [{"@id": "p", "count": 2, "items": items}]})})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert _version(package, "1.0.0").artifacts[0].download_url == "https://cdn.example/demo.pkg.1.0.0.nupkg"
    assert _version(package, "2.0.0").artifacts[0].download_url == \
        "https://api.nuget.org/v3-flatcontainer/demo.pkg/2.0.0/demo.pkg.2.0.0.nupkg"


def test_without_a_flat_container_a_missing_package_content_is_none(serve) -> None:
    feed = "https://feed.example/v3/index.json"
    serve({
        feed: httpx.Response(200, json=_resources(("RegistrationsBaseUrl", "https://feed.example/reg"))),
        "https://feed.example/reg/demo.pkg/index.json": httpx.Response(
            200, json={"items": [{"@id": "p", "items": [{"catalogEntry": _entry()}]}]}),
    })
    package = _fetch(_adapter(_registry(feed)), "demo.pkg")
    assert package is not None and package.versions[0].artifacts[0].download_url is None


# --- dependencies of a document ------------------------------------------------------------------------------------


def test_dependency_fields_exclude_include_and_a_missing_range(serve) -> None:
    serve({DEMO: _document(_entry(dependencyGroups=[
        _group("net8.0", {"id": "Lib.One", "range": "[1.0.0, 2.0.0)", "exclude": "Build,Analyzers", "include": "Compile"}),
        _group("netstandard2.0", {"id": "Lib.Two"}, {"id": "lib.three", "range": "1.0", "include": ""}),
        _group("net6.0"),
    ]))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    one, two, three = package.versions[0].dependencies or []
    groups = ["net8.0", "netstandard2.0", "net6.0"]
    assert (one.target.name, one.constraint, one.raw_spec) == ("lib.one", "[1.0.0, 2.0.0)", "Lib.One [1.0.0, 2.0.0)")
    assert one.environment_markers == {"dotnet_target_framework": "net8.0"}
    assert one.details == {"groups": groups, "exclude": "Build,Analyzers", "include": "Compile"}
    assert (two.target.name, two.constraint, two.raw_spec) == ("lib.two", "", "Lib.Two")
    assert two.environment_markers == {"dotnet_target_framework": "netstandard2.0"} and two.details == {"groups": groups}
    assert (three.target.name, three.constraint, three.raw_spec) == ("lib.three", "1.0", "lib.three 1.0")
    assert three.details == {"groups": groups}   # an empty include is not an include


def test_a_dependency_with_a_malformed_id_is_skipped_not_fatal(serve, caplog) -> None:
    serve({DEMO: _document(_entry(dependencyGroups=[
        _group("net8.0", {"id": "bad id!", "range": "1.0"}, {"id": "Good.Lib", "range": "1.0"}, {"range": "1.0"}),
    ]))})
    with caplog.at_level(logging.WARNING):
        package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert [d.target.name for d in package.versions[0].dependencies or []] == ["good.lib"]
    assert "bad id!" in caplog.text


def test_the_same_dependency_in_several_groups_is_stored_once_per_group(serve) -> None:
    serve({DEMO: _document(_entry(dependencyGroups=[
        _group("net8.0", {"id": "Lib", "range": "[1.0, )"}), _group("net6.0", {"id": "Lib", "range": "[0.9, )"}),
    ]))})
    package = _fetch(_adapter(), "demo.pkg")
    assert package is not None
    assert [(d.constraint, d.environment_markers) for d in package.versions[0].dependencies or []] == [
        ("[1.0, )", {"dotnet_target_framework": "net8.0"}), ("[0.9, )", {"dotnet_target_framework": "net6.0"})]


# --- fetch_version_dependencies ------------------------------------------------------------------------------------


def test_the_dependencies_of_one_version(serve) -> None:
    upstream = serve(_all_fixtures())
    result = _deps(_adapter(), "newtonsoft.json", "13.0.1")
    assert result.available and result.engines is None and len(result.dependencies) == 10
    package = _fetch(_adapter(), "newtonsoft.json")
    assert package is not None
    assert result.dependencies == _version(package, "13.0.1").dependencies
    assert upstream.urls[:2] == [INDEX, NEWTONSOFT]


def test_the_version_is_looked_up_in_its_normalized_form(serve) -> None:
    serve(_all_fixtures())
    assert len(_deps(_adapter(), "newtonsoft.json", "13.0.1.0").dependencies) == 10
    assert len(_deps(_adapter(), "newtonsoft.json", "13.0.1+build.5").dependencies) == 10


def test_an_unknown_version_or_package_has_no_dependencies_available(serve) -> None:
    serve(_all_fixtures())
    assert _deps(_adapter(), "newtonsoft.json", "99.0.0").available is False
    assert _deps(_adapter(), "newtonsoft.json", "not a version").available is False
    gone = _deps(_adapter(), "no.such.package", "1.0.0")
    assert gone.available is False and gone.dependencies == []


def test_the_dependencies_of_a_version_in_an_external_page(serve) -> None:
    serve(_all_fixtures())
    result = _deps(_adapter(), "microsoft.extensions.logging", "3.1.23")
    assert result.available
    assert {(d.environment_markers or {})["dotnet_target_framework"] for d in result.dependencies} == {
        ".NETCoreApp3.1", ".NETStandard2.0"}


# --- test_connection -----------------------------------------------------------------------------------------------


def test_connection_reads_the_service_index(serve) -> None:
    upstream = serve({})
    asyncio.run(_adapter().test_connection())
    assert upstream.urls == [INDEX]


def test_connection_reads_it_again_each_time(serve) -> None:
    upstream = serve({})
    adapter = _adapter()
    asyncio.run(adapter.test_connection())
    asyncio.run(adapter.test_connection())
    assert upstream.urls == [INDEX, INDEX]   # a check must see the feed as it is now, not the cached document


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_connection_fails_on_an_error_status(serve, status) -> None:
    serve({INDEX: httpx.Response(status)})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_adapter().test_connection())


def test_connection_fails_on_a_document_without_resources(serve) -> None:
    serve({INDEX: httpx.Response(200, json={"version": "3.0.0"})})
    with pytest.raises(ValueError, match="resources"):
        asyncio.run(_adapter().test_connection())


def test_connection_fails_on_a_document_that_is_not_an_object(serve) -> None:
    serve({INDEX: httpx.Response(200, json=["resources"])})
    with pytest.raises(ValueError):
        asyncio.run(_adapter().test_connection())


def test_connection_fails_on_a_feed_without_registrations(serve) -> None:
    serve({INDEX: httpx.Response(200, json=_resources(
        ("SearchQueryService", "https://f/query"), ("PackageBaseAddress/3.0.0", "https://f/flat/")))})
    with pytest.raises(ValueError, match="RegistrationsBaseUrl"):
        asyncio.run(_adapter().test_connection())


def test_a_feed_without_registrations_fails_a_fetch_too(serve) -> None:
    serve({INDEX: httpx.Response(200, json={"version": "3.0.0", "resources": []})})
    with pytest.raises(ValueError):
        _fetch(_adapter(), "newtonsoft.json")


# --- get -----------------------------------------------------------------------------------------------------------


def test_transient_errors_are_retried(serve, monkeypatch) -> None:
    answers = [httpx.Response(503), httpx.Response(429), _ok("registration-newtonsoft-json.json", NEWTONSOFT_ETAG)]
    upstream = serve({NEWTONSOFT: lambda request: answers.pop(0)})
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(v3.asyncio, "sleep", sleep)
    adapter = _adapter()
    adapter.max_retries = 3
    assert _fetch(adapter, "newtonsoft.json") is not None
    assert len(slept) == 2 and upstream.urls.count(NEWTONSOFT) == 3


def test_retries_stop_at_the_limit(serve, monkeypatch) -> None:
    upstream = serve({NEWTONSOFT: httpx.Response(503)})

    async def sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(v3.asyncio, "sleep", sleep)
    adapter = _adapter()
    adapter.max_retries = 2
    with pytest.raises(httpx.HTTPStatusError):
        _fetch(adapter, "newtonsoft.json")
    assert upstream.urls.count(NEWTONSOFT) == 3


def test_the_adapter_has_no_interval_of_its_own() -> None:
    assert _adapter().min_interval_seconds() == 0.0


def test_a_version_the_scheme_rejects_raises_the_contract_error() -> None:
    # the adapter skips a version on this error, so the scheme must keep raising it
    with pytest.raises(InvalidVersion):
        v3.normalize("not-a-version")


# --- the change detector -------------------------------------------------------------------------------------------


def _tracked(raw: str, revision: str | None, etag: str | None = None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(ref=_ref(raw), package_id=raw, revision=revision, etag=etag, unavailable=unavailable)


def _conditional(etag: str, new_etag: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == etag and new_etag is None:
            return httpx.Response(304, headers={"etag": etag})
        return _ok("registration-newtonsoft-json.json", new_etag or etag)

    return answer


def test_the_detector_is_the_etag_one_without_a_catalog() -> None:
    assert isinstance(_adapter().change_detector(), NuGetEtagChangeDetector)
    assert isinstance(_adapter(_registry(catalog_url="")).change_detector(), NuGetEtagChangeDetector)
    assert isinstance(_adapter(_registry(catalog_url=None)).change_detector(), NuGetEtagChangeDetector)


def test_the_detector_is_the_catalog_one_with_a_catalog_url() -> None:
    catalog = "https://api.nuget.org/v3/catalog0/index.json"
    adapter = _adapter(_registry(catalog_url=catalog))
    detector = adapter.change_detector()
    assert isinstance(detector, NuGetCatalogChangesDetector)
    assert detector.catalog_url == catalog and detector.registry is adapter.registry
    assert isinstance(detector.reconciler, NuGetEtagChangeDetector) and detector.reconciler.adapter is adapter


def test_detector_asks_conditionally_and_reports_changed_and_missing(serve) -> None:
    upstream = serve({
        NEWTONSOFT: _conditional('"old"', new_etag='"new"'),
        f"{REGISTRATIONS}serilog/index.json": _conditional('"a"'),
        f"{REGISTRATIONS}gone.pkg/index.json": httpx.Response(404),
        f"{REGISTRATIONS}back.pkg/index.json": _conditional('"b"'),
        f"{REGISTRATIONS}still.gone/index.json": httpx.Response(404),
        f"{REGISTRATIONS}by.etag/index.json": _conditional('"e"'),
    })
    tracked = [
        _tracked("newtonsoft.json", '"old"'), _tracked("Serilog", '"a"'), _tracked("gone.pkg", '"g"'),
        _tracked("never-synced", None), _tracked("back.pkg", '"b"', unavailable=True),
        _tracked("still.gone", '"s"', unavailable=True), _tracked("by.etag", None, etag='"e"'),
    ]

    result = asyncio.run(_adapter().change_detector().detect(tracked, {"anything": 1}))

    assert [(p.ref.name, revision) for p, revision in result.changed] == [
        ("newtonsoft.json", '"new"'), ("never-synced", None), ("back.pkg", '"b"')]
    assert [p.ref.name for p in result.missing] == ["gone.pkg"]
    assert result.cursor == {} and not result.reconcile_all
    sent = {str(r.url).removeprefix(REGISTRATIONS): r.headers.get("if-none-match") for r in upstream.requests}
    assert sent["newtonsoft.json/index.json"] == '"old"' and sent["serilog/index.json"] == '"a"'
    assert sent["by.etag/index.json"] == '"e"'   # the ETag column when there is no revision
    assert "never-synced/index.json" not in sent   # without a stored ETag there is nothing to ask conditionally
    assert upstream.urls.count(INDEX) == 1   # read once for the parallel checks


def test_detector_with_nothing_tracked_asks_nobody(serve) -> None:
    upstream = serve({})
    result = asyncio.run(_adapter().change_detector().detect([], {}))
    assert result.changed == [] and result.missing == [] and upstream.urls == []


def test_detector_bounds_the_concurrency(monkeypatch) -> None:
    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        if str(request.url) == INDEX:
            return _ok("service-index.json")
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(304, headers={"etag": '"a"'})

    monkeypatch.setattr(v3, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    tracked = [_tracked(f"pkg{i}", '"a"') for i in range(8)]
    result = asyncio.run(NuGetEtagChangeDetector(_adapter(), concurrency=3).detect(tracked, {}))
    assert result.changed == [] and result.missing == [] and peak == 3


def test_detector_survives_one_failing_package(serve, caplog) -> None:
    serve({NEWTONSOFT: _conditional('"old"', new_etag='"new"'), f"{REGISTRATIONS}broken.pkg/index.json": httpx.Response(503)})
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(_adapter().change_detector().detect(
            [_tracked("newtonsoft.json", '"old"'), _tracked("broken.pkg", '"b"')], {}))
    assert [(p.ref.name, revision) for p, revision in result.changed] == [("newtonsoft.json", '"new"')]
    assert result.missing == [] and "broken.pkg" in caplog.text


def test_detector_survives_a_service_index_that_is_down(serve, caplog) -> None:
    serve({INDEX: httpx.Response(503)})
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(_adapter().change_detector().detect([_tracked("newtonsoft.json", '"old"')], {}))
    assert result.changed == [] and result.missing == [] and "newtonsoft.json" in caplog.text


def test_the_revision_of_a_fetched_package_is_what_the_detector_compares(serve) -> None:
    serve({**_all_fixtures(), NEWTONSOFT: _conditional(NEWTONSOFT_ETAG)})
    adapter = _adapter()
    package = _fetch(adapter, "newtonsoft.json")
    assert package is not None
    result = asyncio.run(adapter.change_detector().detect([_tracked("newtonsoft.json", package.revision)], {}))
    assert result.changed == [] and result.missing == []
