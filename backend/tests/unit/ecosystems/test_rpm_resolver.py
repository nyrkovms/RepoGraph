"""The RPM resolver's side in the API (IMPL K6): the request it sends rpm-worker and how it maps the transaction."""

import asyncio
from typing import Any

import pytest

from app.ecosystems.base import PackageRef, RegistryConfig, ResolveRequest, ResolveRoot, SourceProfile
from app.ecosystems.rpm import client, resolver
from app.ecosystems.rpm.environment import parse_environment


def _registry(slug: str, namespace: str) -> RegistryConfig:
    return RegistryConfig(id=f"id-{slug}", slug=slug, ecosystem="rpm", protocol="rpm-repodata",
                          base_url=f"https://mirror/{slug}/$basearch/os/", priority=0,
                          config={"namespace": namespace, "arch": ["x86_64"]}, routing=None, auth_type="none",
                          credentials_ref=None)


PROFILE = SourceProfile(ecosystem="rpm", slug="default", registries=[
    _registry("rocky-8-baseos", "rocky-8"), _registry("rocky-8-appstream", "rocky-8"), _registry("epel-8", "epel-8")])
NODEJS = "nodejs-1:18.20.4-1.module+el8.10.0+1.x86_64"
OPENSSL = "openssl-libs-1:1.1.1k-12.el8.x86_64"
GLIBC = "glibc-0:2.28-251.el8.x86_64"
ANSWER = {
    "nodes": [{"nevra": GLIBC, "name": "glibc", "evr": "2.28-251.el8", "arch": "x86_64", "repo": "rocky-8-baseos"},
              {"nevra": NODEJS, "name": "nodejs", "evr": "1:18.20.4-1.module+el8.10.0+1", "arch": "x86_64",
               "repo": "rocky-8-appstream"},
              {"nevra": OPENSSL, "name": "openssl-libs", "evr": "1:1.1.1k-12.el8", "arch": "x86_64",
               "repo": "rocky-8-baseos"}],
    "edges": [{"from": NODEJS, "to": OPENSSL, "capability": "libssl.so.1.1()(64bit)", "kind": "requires"},
              {"from": OPENSSL, "to": GLIBC, "capability": "libc.so.6()(64bit)", "kind": "requires"}],
    "roots": [{"spec": "nodejs", "nevra": NODEJS}], "modules": {"nodejs": "18"},
}


@pytest.fixture
def worker(monkeypatch):
    sent: list[Any] = []
    answer: dict[str, Any] = {"value": ANSWER}

    async def call(task: str, *args: Any, timeout: float = 0) -> Any:
        sent.append((task, args, timeout))
        return answer["value"]

    monkeypatch.setattr(client, "call", call)
    queued: list[list[str]] = []
    monkeypatch.setattr(resolver, "_queue_discovered", queued.append)
    return sent, answer, queued


def _request(env: dict | None = None, *roots: str) -> ResolveRequest:
    return ResolveRequest(roots=[ResolveRoot(ref=PackageRef("rpm", "rocky-8", r), constraint="*") for r in roots or ("nodejs",)],
                          environment=parse_environment(env or {"module_streams": {"nodejs": "18"}}), profile=PROFILE,
                          timeout_s=45)


def test_the_transaction_becomes_nodes_by_depth_and_capability_edges(worker) -> None:
    sent, _, queued = worker
    graph = asyncio.run(resolver.RESOLVER.resolve(_request(), None))  # type: ignore[arg-type]
    assert graph.error is None and graph.strategy == "libsolv" and resolver.RESOLVER.emulates == "dnf 4"
    assert [(n.id, n.ref.name, n.depth, n.registry_slug) for n in graph.nodes] == [
        ("n1", "nodejs", 1, "rocky-8-appstream"), ("n2", "openssl-libs", 2, "rocky-8-baseos"),
        ("n3", "glibc", 3, "rocky-8-baseos")]
    assert [(e.source, e.target, e.constraint, e.dependency_type) for e in graph.edges] == [
        ("root", "n1", "*", "runtime"), ("n1", "n2", "libssl.so.1.1()(64bit)", "runtime"),
        ("n2", "n3", "libc.so.6()(64bit)", "runtime")]
    assert graph.details == {"modules": {"nodejs": "18"}, "distro": "rocky-8", "arch": "x86_64"}
    task, (payload,), timeout = sent[0]
    assert task == "rpm.resolve" and timeout == 45
    assert [s["slug"] for s in payload["sources"]] == ["rocky-8-baseos", "rocky-8-appstream"]   # not epel-8
    assert payload["roots"] == [{"spec": "nodejs", "constraint": ""}] and payload["module_streams"] == {"nodejs": "18"}
    assert queued == [["pkg:rpm/rocky/glibc?distro=rocky-8", "pkg:rpm/rocky/nodejs?distro=rocky-8",
                       "pkg:rpm/rocky/openssl-libs?distro=rocky-8"]]


def test_repos_narrow_the_pool_and_problems_become_the_error(worker) -> None:
    sent, answer, _ = worker
    answer["value"] = {"problems": [{"code": "module_conflict", "module": "perl", "message": "module perl: 5.26, 5.32"}]}
    graph = asyncio.run(resolver.RESOLVER.resolve(_request({"repos": ["rocky-8-appstream"]}), None))  # type: ignore[arg-type]
    assert graph.error is not None and graph.error["code"] == "module_conflict" and graph.conflicts
    assert [s["slug"] for s in sent[0][1][0]["sources"]] == ["rocky-8-appstream"]


def test_a_distro_without_sources_or_roots_of_two_distros(worker) -> None:
    graph = asyncio.run(resolver.RESOLVER.resolve(_request({"distro": "fedora-41"}), None))  # type: ignore[arg-type]
    assert graph.error is not None and graph.error["code"] == "not_found"
    mixed = ResolveRequest(roots=[ResolveRoot(PackageRef("rpm", "rocky-8", "a"), "*"),
                                  ResolveRoot(PackageRef("rpm", "epel-8", "b"), "*")],
                           environment=parse_environment({}), profile=PROFILE)
    assert asyncio.run(resolver.RESOLVER.resolve(mixed, None)).error["code"] == "invalid_request"  # type: ignore[arg-type,index]
