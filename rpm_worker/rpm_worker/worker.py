"""Celery tasks of the `rpm` queue (SPEC-MULTI-ECOSYSTEM.md §3.2). The API's `rpm-repodata` adapter calls them by name
and waits for the result; arguments and results are plain JSON.

A source argument is the registry as the API sends it (`id`, `slug`, `base_url`, `auth_type`, `credentials_ref`,
`config` with `namespace`, `arch`, `releasever`, `modular`).
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

from celery import Celery

from rpm_worker import http, repo

logger = logging.getLogger(__name__)

broker = os.environ.get("REDIS_URL", "redis://redis:6379/0")
app = Celery("rpm_worker", broker=broker, backend=broker)
app.conf.update(task_default_queue="rpm", task_serializer="json", result_serializer="json", accept_content=["json"],
                result_expires=3600, worker_prefetch_multiplier=1)


def fingerprint(builds: List[Mapping[str, Any]]) -> str:
    """A package's revision in a source: its builds (NEVRA and checksum), whatever their order."""
    keys = sorted("%s-%s.%s %s" % (b["name"], b["evr"], b["arch"], (b.get("checksum") or {}).get("value", ""))
                  for b in builds)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _snapshots(source: repo.Source) -> Dict[str, repo.Snapshot]:
    client = http.make_client(auth_type=source.auth_type, credentials_ref=source.credentials_ref)
    try:
        return {arch: repo.sync(source, arch, client) for arch in source.arches}
    finally:
        client.close()


def _builds(source: repo.Source, snapshots: Mapping[str, repo.Snapshot], name: str) -> List[Dict[str, Any]]:
    from rpm_worker import pools
    found: Dict[str, Dict[str, Any]] = {}
    for arch, snapshot in snapshots.items():
        pool, _ = pools.make_pool(arch, [(source.slug, snapshot.solv)])
        for build in pools.builds(pool, name):
            # noarch builds show up under every arch: one entry per NEVRA, with the arches whose repo lists it.
            key = "%s-%s.%s" % (build["name"], build["evr"], build["arch"])
            found.setdefault(key, dict(build, repo_arches=[]))["repo_arches"].append(arch)
    return sorted(found.values(), key=lambda b: (b["arch"], b["evr"]))


@app.task(name="rpm.sync_repo")
def sync_repo(source: Mapping[str, Any]) -> Dict[str, Any]:
    src = repo.source_from(source)
    try:
        snapshots = _snapshots(src)
    except repo.Missing as exc:
        return {"missing": True, "url": str(exc)}
    return {"missing": False, "revisions": {arch: s.revision for arch, s in snapshots.items()}}


@app.task(name="rpm.fetch_package")
def fetch_package(source: Mapping[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """The builds of a package in the source, None when the source has none (or no repository at all)."""
    src = repo.source_from(source)
    try:
        snapshots = _snapshots(src)
    except repo.Missing:
        return None
    builds = _builds(src, snapshots, name)
    if not builds:
        return None
    from rpm_worker import modules
    return {"builds": builds, "revision": fingerprint(builds),
            "modules": modules.artifact_streams(snapshots.values(), [b for b in builds])}


@app.task(name="rpm.detect_changes")
def detect_changes(source: Mapping[str, Any], tracked: Mapping[str, Optional[str]]) -> Dict[str, Any]:
    """For `{name: stored revision}`: the names whose builds changed (with the new revision) and those gone."""
    src = repo.source_from(source)
    try:
        snapshots = _snapshots(src)
    except repo.Missing:
        return {"changed": {}, "missing": sorted(tracked), "revisions": {}}
    changed: Dict[str, str] = {}
    missing: List[str] = []
    for name, known in tracked.items():
        builds = _builds(src, snapshots, name)
        if not builds:
            missing.append(name)
            continue
        now = fingerprint(builds)
        if now != known:
            changed[name] = now
    return {"changed": changed, "missing": missing, "revisions": {a: s.revision for a, s in snapshots.items()}}


@app.task(name="rpm.test_connection")
def test_connection(source: Mapping[str, Any]) -> Dict[str, Any]:
    src = repo.source_from(source)
    return {"revision": repo.revision(src)}


@app.task(name="rpm.resolve")
def resolve(request: Mapping[str, Any]) -> Dict[str, Any]:
    from rpm_worker import solver
    return solver.resolve(request)


@app.task(name="rpm.whatprovides")
def whatprovides(sources: List[Mapping[str, Any]], arch: str, capability: str) -> List[Dict[str, Any]]:
    from rpm_worker import pools
    pairs = []
    for raw in sources:
        src = repo.source_from(raw)
        if arch not in src.arches:
            continue
        try:
            pairs.append((src.slug, repo.sync(src, arch).solv))
        except repo.Missing:
            continue
    pool = pools.cached_pool(arch, pairs)
    return pools.whatprovides(pool, capability)
