import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.connectors import base as legacy
from app.ecosystems.base import NormalizedDependency, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.pypi.adapters import WarehouseAdapter, to_contract
from app.ecosystems.pypi.changes import WarehouseChangeDetector, plan_changes
from app.ecosystems.pypi.environment import dependency_applies, parse_environment
from app.ecosystems.registry import get_plugin, load_builtin_plugins

REGISTRY = RegistryConfig(
    id="reg-pypi", slug="pypi", ecosystem="pypi", protocol="pypi-warehouse", base_url="https://pypi.org/",
    priority=0, config={}, routing=None, auth_type="none", credentials_ref=None,
)


def _tracked(name: str, revision: str | None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(
        ref=PackageRef(ecosystem="pypi", namespace="", name=name), package_id=name, revision=revision, etag=None,
        unavailable=unavailable,
    )


def test_plugin_is_registered() -> None:
    load_builtin_plugins()
    plugin = get_plugin("pypi")
    assert plugin.coordinates.parse("Typing_Extensions").name == "typing-extensions"
    assert type(plugin.adapter(REGISTRY)).__name__ == "WarehouseAdapter"


# --- change detection: same cases as plan_updates (tests/unit/test_daily_update_plan.py) -----------------------


def test_plan_changes_selects_only_changed_and_missing() -> None:
    tracked = [
        _tracked("flask", "100"),
        _tracked("django", "200"),
        _tracked("never-synced", None),
        _tracked("gone", "50"),
        _tracked("already-gone", "60", unavailable=True),
        _tracked("back-again", "70", unavailable=True),
    ]
    index = {"flask": 150, "django": 200, "never-synced": 10, "back-again": 70, "not-in-catalog": 999}

    result = plan_changes(tracked, index)

    assert [(package.ref.name, serial) for package, serial in result.changed] == [
        ("flask", "150"),
        ("never-synced", "10"),
        ("back-again", "70"),
    ]
    assert [package.ref.name for package in result.missing] == ["gone"]


def test_plan_changes_ignores_lower_serial_from_cached_index() -> None:
    result = plan_changes([_tracked("flask", "150")], {"flask": 140})
    assert result.changed == [] and result.missing == []


class _Index:
    def __init__(self, fail_root: bool) -> None:
        self.fail_root = fail_root

    async def fetch_all_serials(self) -> tuple[int | None, dict[str, int]]:
        if self.fail_root:
            raise RuntimeError("root index unavailable")
        return 999, {"flask": 150}

    async def fetch_project_serial(self, name: str) -> int | None:
        return {"flask": 150}.get(name)


def test_detector_falls_back_to_per_project_serials() -> None:
    detector = WarehouseChangeDetector(index=_Index(fail_root=True))  # type: ignore[arg-type]
    result = asyncio.run(detector.detect([_tracked("flask", "100"), _tracked("gone", "5")], {}))

    assert [package.ref.name for package, _ in result.changed] == ["flask"]
    assert [package.ref.name for package in result.missing] == ["gone"]
    assert result.cursor == {"index_serial": None}


def test_detector_records_index_serial_in_cursor() -> None:
    detector = WarehouseChangeDetector(index=_Index(fail_root=False))  # type: ignore[arg-type]
    result = asyncio.run(detector.detect([_tracked("flask", "100")], {"other": 1}))
    assert result.cursor == {"other": 1, "index_serial": 999}


# --- conversion to the contract types ---------------------------------------------------------------------------


def _legacy_package() -> legacy.NormalizedPackage:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    dep = legacy.NormalizedDependency(
        dependency_name="Jinja2", version_constraint=">=3.1.2", dependency_type="runtime", extra_name=None,
        environment_markers=None, raw_spec="Jinja2>=3.1.2",
    )
    hydrated = legacy.NormalizedVersion(
        version="3.1.0", release_date=now, status="active", requires_python=">=3.9", raw_metadata={},
        dependencies_hydrated=True, dependencies=[dep],
        artifacts=[legacy.NormalizedArtifact(
            filename="flask-3.1.0-py3-none-any.whl", download_url="https://files/flask.whl", size_bytes=10,
            checksum_sha256="a" * 64, checksum_md5=None, artifact_type="wheel", platform_tags=None,
            requires_dist_metadata=True,
        )],
    )
    lazy = legacy.NormalizedVersion(
        version="3.0.0", release_date=now, status="yanked", requires_python=None, raw_metadata={},
        yanked_reason="broken",
    )
    return legacy.NormalizedPackage(
        ecosystem="pypi", name="Flask", normalized_name="flask", description="d", homepage_url=None,
        repository_url=None, documentation_url="https://docs", license_raw="BSD", license_spdx="BSD-3-Clause",
        keywords=None, latest_version="3.1.0", downloads_total=None, downloads_recent=None, created_at=now,
        updated_at=now, raw_metadata={"info": {}}, versions=[hydrated, lazy],
        authors=[legacy.NormalizedAuthor(name="Pallets", email=None, url=None, role="maintainer")],
        vulnerabilities=[legacy.NormalizedVulnerability(
            source="osv", source_id="PYSEC-1", aliases=[], summary="s", details=None, severity=None, fixed_in=None,
            published_at=None, withdrawn_at=None, link="https://osv.dev",
        )],
        last_serial=42,
    )


def test_to_contract_keeps_everything_the_pipeline_writes() -> None:
    package = to_contract(_legacy_package(), "reg-pypi")

    assert package.ref == PackageRef(ecosystem="pypi", namespace="", name="flask")
    assert (package.display_name, package.revision, package.registry_id) == ("Flask", "42", "reg-pypi")
    assert package.documentation_url == "https://docs"
    assert [a.name for a in package.authors or []] == ["Pallets"]
    assert [v.source_id for v in package.vulnerabilities or []] == ["PYSEC-1"]

    hydrated, lazy = package.versions
    assert hydrated.engines == {"python": ">=3.9"}
    assert [d.target.name for d in hydrated.dependencies or []] == ["jinja2"]
    assert hydrated.artifacts[0].checksums == {"sha256": "a" * 64}
    assert hydrated.artifacts[0].has_metadata_file
    assert lazy.dependencies is None  # not loaded: stays lazy
    assert (lazy.status, lazy.status_reason) == ("yanked", "broken")


class _Connector:
    async def fetch_package_metadata(self, name: str) -> legacy.NormalizedPackage:
        if name != "flask":
            raise legacy.PackageNotFound(name)
        return _legacy_package()

    def get_rate_limit(self) -> float:
        return 0.0


def test_adapter_returns_none_for_missing_package() -> None:
    adapter = WarehouseAdapter(REGISTRY, connector=_Connector())  # type: ignore[arg-type]
    ref = PackageRef(ecosystem="pypi", namespace="", name="nope")
    assert asyncio.run(adapter.exists(ref)) is False
    assert asyncio.run(adapter.exists(PackageRef(ecosystem="pypi", namespace="", name="flask"))) is True


class _UnauthorizedConnector(_Connector):
    async def fetch_package_metadata(self, name: str) -> legacy.NormalizedPackage:
        request = httpx.Request("GET", f"https://pypi.invalid/pypi/{name}/json")
        raise httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))


def _simple_root(monkeypatch, status: int) -> list[str]:
    from app.ecosystems.pypi.adapters import warehouse as adapters

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(status, text="<html></html>")

    monkeypatch.setattr(adapters, "make_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


def test_connection_check_fails_only_on_source_errors(monkeypatch) -> None:
    # Without the probe project, a reachable index still serves its Simple root.
    asked = _simple_root(monkeypatch, 200)
    asyncio.run(WarehouseAdapter(REGISTRY, connector=_Connector()).test_connection())  # type: ignore[arg-type]
    assert asked == ["https://pypi.org/simple/"]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(WarehouseAdapter(REGISTRY, connector=_UnauthorizedConnector()).test_connection())  # type: ignore[arg-type]


def test_connection_check_fails_on_a_wrong_base_url(monkeypatch) -> None:
    # Everything 404s: no `pip` and no Simple root, so the source is not what base_url claims.
    _simple_root(monkeypatch, 404)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(WarehouseAdapter(REGISTRY, connector=_Connector()).test_connection())  # type: ignore[arg-type]


# --- environment: same semantics as the tree endpoint ------------------------------------------------------------


def _dep(dep_type: str, expression: str | None, conditions: list[dict[str, str]] | None = None) -> NormalizedDependency:
    markers = None if expression is None else {"type": "python_environment", "expression": expression,
                                               "conditions": conditions or []}
    return NormalizedDependency(
        target=PackageRef(ecosystem="pypi", namespace="", name="x"), constraint="*",
        dependency_type=dep_type,  # type: ignore[arg-type]
        raw_spec="x", environment_markers=markers,
    )


def test_markers_follow_the_tree_endpoint_semantics() -> None:
    py39 = parse_environment({"python_version": "3.9", "os": "linux"})
    py313 = parse_environment({"python_version": "3.13", "os": "windows"})
    old_python = _dep("runtime", 'python_version < "3.10"', [{"marker": "python_version", "op": "<", "value": "3.10"}])
    test_extra = _dep(
        "optional", 'python_version >= "3.8" and extra == "test"',
        [{"marker": "python_version", "op": ">=", "value": "3.8"}, {"marker": "extra", "op": "==", "value": "test"}],
    )

    assert dependency_applies(old_python, py39, include_optional=False)
    assert not dependency_applies(old_python, py313, include_optional=False)
    # Optional dependencies only with include_optional; then `extra` conditions are ignored.
    assert not dependency_applies(test_extra, py39, include_optional=False)
    assert dependency_applies(test_extra, py39, include_optional=True)
    # No environment given: markers are not evaluated.
    assert dependency_applies(old_python, parse_environment({}), include_optional=False)
    assert not dependency_applies(_dep("dev", None), py39, include_optional=True)


# --- parity with the tree endpoint (api/v1/package_detail.py), which B1/C1 replace with the plugin ----------------------

PARITY_ENVIRONMENTS = [
    {},
    {"python_version": None, "os": None},
    {"python_version": "3.9"},
    {"os": "windows"},
    {"python_version": "3.13", "os": "linux"},
    {"python_version": "3.11", "os": "darwin"},
]
PARITY_MARKERS = [
    ('python_version < "3.10"', [{"marker": "python_version", "op": "<", "value": "3.10"}]),
    ('sys_platform == "linux"', [{"marker": "sys_platform", "op": "==", "value": "linux"}]),
    ('platform_system != "Windows"', [{"marker": "platform_system", "op": "!=", "value": "Windows"}]),
    ('python_version >= "3.8" and extra == "test"',
     [{"marker": "python_version", "op": ">=", "value": "3.8"}, {"marker": "extra", "op": "==", "value": "test"}]),
    ('sys_platform in "linux darwin"', []),  # `in` is not split into conditions
    ('extra == "docs"', [{"marker": "extra", "op": "==", "value": "docs"}]),
]


def test_marker_semantics_match_the_tree_endpoint() -> None:
    import pytest

    from app.api.v1.package_detail import _build_marker_environment, _tree_marker_applies

    for raw_env in PARITY_ENVIRONMENTS:
        old_env = _build_marker_environment(raw_env.get("python_version"), raw_env.get("os"))
        new_env = parse_environment(raw_env)
        for expression, conditions in PARITY_MARKERS:
            markers = {"type": "python_environment", "expression": expression, "conditions": conditions}
            for include_optional in (False, True):
                expected = _tree_marker_applies(markers, old_env, include_optional)
                dep = _dep("optional" if include_optional else "runtime", expression, conditions)
                if dependency_applies(dep, new_env, include_optional) != expected:
                    pytest.fail(f"{raw_env} {expression!r} include_optional={include_optional}: expected {expected}")


def test_warehouse_source_reads_from_its_own_urls(monkeypatch) -> None:
    from dataclasses import replace

    from app.config import get_settings

    # pypi.org follows the settings (README: a proxy in front of pypi.org).
    monkeypatch.setattr(get_settings(), "sync_pypi_json_base_url", "https://proxy.invalid/pypi/")
    adapter = WarehouseAdapter(REGISTRY)
    assert adapter.connector.json_base_url == "https://proxy.invalid/pypi"

    mirror = replace(REGISTRY, id="reg-mirror", slug="mirror", base_url="https://mirror.invalid/root/")
    adapter = WarehouseAdapter(mirror)
    assert adapter.connector.json_base_url == "https://mirror.invalid/root/pypi"
    assert adapter.change_detector().index.base_url == "https://mirror.invalid/root/simple"  # type: ignore[attr-defined]

    custom = replace(mirror, config={"api_url": "https://mirror.invalid/json/", "simple_url": "https://mirror.invalid/idx"})
    adapter = WarehouseAdapter(custom)
    assert adapter.connector.json_base_url == "https://mirror.invalid/json"
    assert adapter.change_detector().index.base_url == "https://mirror.invalid/idx"  # type: ignore[attr-defined]


@pytest.mark.parametrize("raw", ["flask/3", "flask/3.0", "../flask", "a b", "-flask", "flask-", "fl@sk"])
def test_coordinates_reject_what_is_not_a_project_name(raw: str) -> None:
    # A slash would reach the JSON API as another endpoint (/pypi/<project>/<version>/json).
    with pytest.raises(ValueError, match="not a PyPI project name"):
        _plugin().coordinates.parse(raw)


@pytest.mark.parametrize(("raw", "name"), [("zope.interface", "zope-interface"), ("A_b-C", "a-b-c"), ("x", "x")])
def test_coordinates_accept_project_names(raw: str, name: str) -> None:
    assert _plugin().coordinates.parse(raw).name == name


def _plugin():
    load_builtin_plugins()
    return get_plugin("pypi")
