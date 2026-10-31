"""The `rpm-repodata` adapter, its change detector and the RPM plugin (IMPL K4) over canned rpm-worker answers."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ecosystems.base import NormalizedDependency, PackageRef, RegistryConfig, SourceProfile, TrackedPackage
from app.ecosystems.rpm import client
from app.ecosystems.rpm.adapters.repodata import RepodataAdapter, split_capability
from app.ecosystems.rpm.environment import RpmEnvironment, dependency_applies, parse_environment
from app.ecosystems.rpm.plugin import PLUGIN

OPENSSL = PackageRef("rpm", "rocky-9", "openssl")


def _registry(slug: str = "rocky-9-baseos", namespace: str = "rocky-9", routing: dict | None = None) -> RegistryConfig:
    return RegistryConfig(id=f"id-{slug}", slug=slug, ecosystem="rpm", protocol="rpm-repodata",
                          base_url="https://dl.rockylinux.org/pub/rocky/9/BaseOS/$basearch/os/", priority=0,
                          config={"namespace": namespace, "arch": ["x86_64", "aarch64"], "releasever": "9"},
                          routing=routing, auth_type="none", credentials_ref=None)


def _build(evr: str, arch: str, requires: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    epoch, _, rest = evr.rpartition(":") if ":" in evr else ("", "", evr)
    version, _, release = rest.partition("-")
    return {"name": "openssl", "evr": evr, "epoch": int(epoch) if epoch else None, "version": version,
            "release": release, "arch": arch, "summary": "TLS toolkit", "description": "The OpenSSL toolkit",
            "license": "Apache-2.0", "url": "https://www.openssl.org/", "buildtime": 1700000000,
            "sourcerpm": f"openssl-{rest}.src.rpm", "location": f"Packages/o/openssl-{rest}.{arch}.rpm",
            "checksum": {"type": "sha256", "value": "ab" * 32}, "size": 1234, "provides": [f"openssl = {evr}"],
            "requires": requires or ["libc.so.6()(64bit)", f"openssl-libs = {evr}"], "recommends": [],
            "suggests": [], "supplements": [], "enhances": [], "repo_arches": [arch], **extra}


ANSWER = {"revision": "rev-1", "modules": {}, "builds": [
    _build("1:3.0.7-27.el9", "x86_64"), _build("1:3.0.7-27.el9", "aarch64", ["libc.so.6()(64bit)"]),
    _build("1:3.0.7-28.el9", "x86_64", recommends=["crypto-policies-scripts"]),
]}


@pytest.fixture
def worker(monkeypatch):
    calls: list[tuple[str, tuple[Any, ...]]] = []
    answers: dict[str, Any] = {}

    async def call(task: str, *args: Any, timeout: float = 0) -> Any:
        calls.append((task, args))
        return answers.get(task)

    monkeypatch.setattr(client, "call", call)
    return calls, answers


def test_split_capability() -> None:
    assert split_capability("openssl-libs(x86-64) = 1:3.0.7-27.el9") == ("openssl-libs(x86-64)", "= 1:3.0.7-27.el9")
    assert split_capability("glibc >= 2.34") == ("glibc", ">= 2.34")
    assert split_capability("/usr/bin/sh") == ("/usr/bin/sh", "")
    assert split_capability("(pkgA if pkgB)") == ("(pkgA if pkgB)", "")


def test_builds_become_versions_with_superseded_status_artifacts_and_arch_markers(worker) -> None:
    _, answers = worker
    answers["rpm.fetch_package"] = ANSWER
    package = asyncio.run(RepodataAdapter(_registry()).fetch_package(OPENSSL, None))  # type: ignore[arg-type]
    assert package is not None and package.revision == "rev-1" and package.display_name == "rocky-9/openssl"
    assert package.license_raw == "Apache-2.0" and package.homepage_url == "https://www.openssl.org/"
    versions = {v.version: v for v in package.versions}
    assert [v.version for v in package.versions] == ["1:3.0.7-27.el9", "1:3.0.7-28.el9"]
    # -27 is still the newest aarch64 build: not superseded while one of its arches has nothing newer.
    assert versions["1:3.0.7-27.el9"].status == "active" and versions["1:3.0.7-28.el9"].status == "active"
    answers["rpm.fetch_package"] = {**ANSWER, "builds": ANSWER["builds"][:1] + ANSWER["builds"][2:]}
    only_x86 = asyncio.run(RepodataAdapter(_registry()).fetch_package(OPENSSL, None))  # type: ignore[arg-type]
    assert only_x86 is not None and {v.version: v.status for v in only_x86.versions} == {
        "1:3.0.7-27.el9": "superseded", "1:3.0.7-28.el9": "active"}
    old = versions["1:3.0.7-27.el9"]
    assert [(a.arch, a.filename, a.download_url) for a in old.artifacts] == [
        ("x86_64", "openssl-3.0.7-27.el9.x86_64.rpm",
         "https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/Packages/o/openssl-3.0.7-27.el9.x86_64.rpm"),
        ("aarch64", "openssl-3.0.7-27.el9.aarch64.rpm",
         "https://dl.rockylinux.org/pub/rocky/9/BaseOS/aarch64/os/Packages/o/openssl-3.0.7-27.el9.aarch64.rpm")]
    assert old.artifacts[0].checksums == {"sha256": "ab" * 32} and old.release_date == datetime.fromtimestamp(1700000000, UTC)
    deps = {(d.target.name, d.constraint): d for d in old.dependencies or []}
    assert deps[("libc.so.6()(64bit)", "")].environment_markers is None
    assert deps[("openssl-libs", "= 1:3.0.7-27.el9")].environment_markers == {"rpm_arch": ["x86_64"]}
    weak = [d for d in versions["1:3.0.7-28.el9"].dependencies or [] if d.dependency_type == "optional"]
    assert [(d.target.name, d.scope, d.details) for d in weak] == [
        ("crypto-policies-scripts", "recommends", {"kind": "recommends"})]
    assert old.raw_metadata["arches"] == ["aarch64", "x86_64"] and old.raw_metadata["sourcerpm"]


def test_a_source_of_another_distro_is_not_asked(worker) -> None:
    calls, _ = worker
    assert asyncio.run(RepodataAdapter(_registry(namespace="epel-9")).fetch_package(OPENSSL, None)) is None  # type: ignore[arg-type]
    assert calls == []


def test_the_detector_maps_the_worker_answer(worker) -> None:
    calls, answers = worker
    answers["rpm.detect_changes"] = {"changed": {"openssl": "rev-2"}, "missing": ["gone", "away"], "revisions": {"x86_64": "r"}}

    def tracked(name: str, unavailable: bool = False) -> TrackedPackage:
        return TrackedPackage(ref=PackageRef("rpm", "rocky-9", name), package_id=name, revision="rev-1", etag=None,
                              unavailable=unavailable)

    packages = [tracked("openssl"), tracked("bash"), tracked("gone"), tracked("away", unavailable=True),
                tracked("back", unavailable=True), TrackedPackage(PackageRef("rpm", "epel-9", "x"), "x", "r", None, False)]
    result = asyncio.run(RepodataAdapter(_registry()).change_detector().detect(packages, {}))
    assert [(p.ref.name, r) for p, r in result.changed] == [("openssl", "rev-2"), ("back", "rev-1")]
    assert [p.ref.name for p in result.missing] == ["gone"]
    assert calls[0][1][1] == {"openssl": "rev-1", "bash": "rev-1", "gone": "rev-1", "away": "rev-1", "back": "rev-1"}


def test_the_plugin_routes_by_distro_and_package_patterns() -> None:
    baseos, epel = _registry(), _registry("epel-9", "epel-9")
    internal = _registry("internal", routing={"mode": "exclusive", "packages": ["corp-*"]})
    profile = SourceProfile(ecosystem="rpm", slug="default", registries=[baseos, epel, internal])
    assert [r.slug for r in PLUGIN.sources_for(OPENSSL, profile)] == ["rocky-9-baseos"]
    assert [r.slug for r in PLUGIN.sources_for(PackageRef("rpm", "rocky-9", "corp-agent"), profile)] == ["internal"]
    assert [r.slug for r in PLUGIN.sources_for(PackageRef("rpm", "epel-9", "htop"), profile)] == ["epel-9"]


def test_dependency_target_asks_the_worker_for_the_provider(worker) -> None:
    calls, answers = worker
    answers["rpm.whatprovides"] = [{"name": "openssl-libs", "evr": "1:3.0.7-27.el9", "arch": "x86_64"}]
    dep = NormalizedDependency(target=PackageRef("rpm", "rocky-9", "libssl.so.3()(64bit)"), constraint="",
                               dependency_type="runtime", raw_spec="libssl.so.3()(64bit)")
    profile = SourceProfile(ecosystem="rpm", slug="default", registries=[_registry(), _registry("epel-9", "epel-9")])
    target = asyncio.run(PLUGIN.dependency_target(dep, parse_environment({"arch": "aarch64"}), profile))
    assert target == PackageRef("rpm", "rocky-9", "openssl-libs")
    task, args = calls[0]
    assert task == "rpm.whatprovides" and [s["slug"] for s in args[0]] == ["rocky-9-baseos"] and args[1] == "aarch64"


def test_environment_and_dependency_filter() -> None:
    env = parse_environment({"arch": "aarch64", "module_streams": {"nodejs": "18"}, "repos": "a,b", "weak_deps": "false"})
    assert env.arch == "aarch64" and env.module_streams == {"nodejs": "18"} and env.repos == ("a", "b") and not env.weak_deps
    assert parse_environment({}).specified is False
    for bad in ({"arch": "sparc"}, {"module_streams": "nodejs:18"}):
        with pytest.raises(ValueError):
            parse_environment(bad)
    weak = NormalizedDependency(target=OPENSSL, constraint="", dependency_type="optional", raw_spec="x")
    x86 = NormalizedDependency(target=OPENSSL, constraint="", dependency_type="runtime", raw_spec="x",
                               environment_markers={"rpm_arch": ["x86_64"]})
    assert not dependency_applies(weak, RpmEnvironment(), include_optional=False)
    assert dependency_applies(weak, RpmEnvironment(), include_optional=True)
    assert not dependency_applies(weak, RpmEnvironment(weak_deps=False), include_optional=True)
    assert dependency_applies(x86, RpmEnvironment(), False) and not dependency_applies(x86, RpmEnvironment(arch="aarch64"), False)


def test_superseded_compares_within_a_module_stream_and_counts_noarch_against_every_arch(worker) -> None:
    _, answers = worker
    node = lambda evr, arch="x86_64", **extra: dict(_build(evr, arch), name="nodejs", **extra)  # noqa: E731
    answers["rpm.fetch_package"] = {"revision": "r", "builds": [
        node("1:18.20.4-1.module+el8.10.0+1"), node("1:18.20.2-1.module+el8.9.0+1"),
        node("1:20.16.0-1.module+el8.10.0+2"), node("1:10.24.0-1.el8"),
    ], "modules": {"nodejs-1:18.20.4-1.module+el8.10.0+1.x86_64": ["nodejs:18"],
                   "nodejs-1:18.20.2-1.module+el8.9.0+1.x86_64": ["nodejs:18"],
                   "nodejs-1:20.16.0-1.module+el8.10.0+2.x86_64": ["nodejs:20"]}}
    package = asyncio.run(RepodataAdapter(_registry()).fetch_package(PackageRef("rpm", "rocky-9", "nodejs"), None))  # type: ignore[arg-type]
    assert package is not None and {v.version: v.status for v in package.versions} == {
        "1:10.24.0-1.el8": "active", "1:18.20.2-1.module+el8.9.0+1": "superseded",
        "1:18.20.4-1.module+el8.10.0+1": "active", "1:20.16.0-1.module+el8.10.0+2": "active"}
    assert package.versions[2].raw_metadata["modules"] == ["nodejs:18"]
    # The newest build turned noarch: it supersedes the older x86_64 one, which dnf would no longer pick.
    answers["rpm.fetch_package"] = {"revision": "r", "modules": {}, "builds": [
        _build("1.0-1.el9", "x86_64"), _build("2.0-1.el9", "noarch", repo_arches=["x86_64", "aarch64"])]}
    turned = asyncio.run(RepodataAdapter(_registry()).fetch_package(OPENSSL, None))  # type: ignore[arg-type]
    assert turned is not None and {v.version: v.status for v in turned.versions} == {
        "1.0-1.el9": "superseded", "2.0-1.el9": "active"}


def test_the_detector_skips_comparisons_only_while_the_source_config_is_unchanged(worker) -> None:
    calls, answers = worker
    answers["rpm.detect_changes"] = {"changed": {}, "missing": [], "revisions": {"x86_64": "r1"}}
    detector = RepodataAdapter(_registry()).change_detector()
    package = TrackedPackage(ref=OPENSSL, package_id="p", revision="rev-1", etag=None, unavailable=False)
    first = asyncio.run(detector.detect([package], {}))
    assert calls[-1][1][2] == {} and first.cursor["revisions"] == {"x86_64": "r1"}
    asyncio.run(detector.detect([package], first.cursor))
    assert calls[-1][1][2] == {"x86_64": "r1"}
    moved = RepodataAdapter(_registry("rocky-9-baseos-mirror")).change_detector()
    asyncio.run(moved.detect([package], first.cursor))
    assert calls[-1][1][2] == {}                                         # another configuration: compare again


def test_the_provider_comes_with_the_evr_of_its_newest_build(worker) -> None:
    calls, answers = worker
    answers["rpm.whatprovides"] = [{"name": "python3-libs", "evr": "3.9.19-1.el9", "arch": "x86_64"},
                                   {"name": "python3-libs", "evr": "3.9.18-3.el9", "arch": "x86_64"}]
    dep = NormalizedDependency(target=PackageRef("rpm", "rocky-9", "python(abi)"), constraint="= 3.9",
                               dependency_type="runtime", raw_spec="python(abi) = 3.9")
    profile = SourceProfile(ecosystem="rpm", slug="default", registries=[_registry()])
    found = asyncio.run(PLUGIN.provider(dep, parse_environment({}), profile))
    assert found == (PackageRef("rpm", "rocky-9", "python3-libs"), "3.9.19-1.el9")
    assert calls[-1][1][2] == "python(abi) = 3.9"                       # the constraint goes to libsolv with the name


def test_waiting_on_the_worker_is_allowed_inside_a_celery_task(monkeypatch) -> None:
    """The daily update, fetch_packages and seed run in the backend's Celery worker, where a bare result.get() raises
    'Never call result.get() within a task!' (C1 of the E6 review)."""
    from celery import _state
    from celery.result import assert_will_not_block

    class Result:
        def get(self, timeout: float, propagate: bool = True, disable_sync_subtasks: bool = True) -> Any:
            if disable_sync_subtasks:
                assert_will_not_block()
            return {"revision": "r"}

        def forget(self) -> None:
            pass

    class App:
        def send_task(self, name: str, args: list, queue: str) -> Result:
            assert queue == ("rpm-batch" if name == "rpm.detect_changes" else "rpm")
            return Result()

    import app.celery_app as celery_module
    monkeypatch.setattr(celery_module, "celery_app", App())
    _state._set_task_join_will_block(True)
    try:
        assert asyncio.run(client.call("rpm.test_connection", {})) == {"revision": "r"}
        assert asyncio.run(client.call("rpm.detect_changes", {}, {}, {})) == {"revision": "r"}
    finally:
        _state._set_task_join_will_block(False)


def test_an_epel_dependency_is_provided_from_its_base_distro(worker) -> None:
    calls, answers = worker
    answers["rpm.whatprovides"] = [{"name": "glibc", "evr": "2.34-100.el9", "arch": "x86_64", "repo": "rocky-9-baseos"}]
    epel = RegistryConfig(id="id-epel-9", slug="epel-9", ecosystem="rpm", protocol="rpm-repodata",
                          base_url="https://mirror/epel/9/$basearch/", priority=40, routing=None, auth_type="none",
                          credentials_ref=None, config={"namespace": "epel-9", "arch": ["x86_64"], "base_distro": ["rocky-9"]})
    profile = SourceProfile(ecosystem="rpm", slug="default", registries=[_registry(), epel])
    dep = NormalizedDependency(target=PackageRef("rpm", "epel-9", "libc.so.6()(64bit)"), constraint="",
                               dependency_type="runtime", raw_spec="libc.so.6()(64bit)")
    found = asyncio.run(PLUGIN.provider(dep, parse_environment({}), profile))
    assert found == (PackageRef("rpm", "rocky-9", "glibc"), "2.34-100.el9")
    assert [s["slug"] for s in calls[-1][1][0]] == ["epel-9", "rocky-9-baseos"]
