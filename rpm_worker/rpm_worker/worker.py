"""Celery tasks of rpm-worker (SPEC-MULTI-ECOSYSTEM.md §3.2). The API's `rpm-repodata` adapter calls them by name and
waits for the result; arguments and results are plain JSON.

Two queues: `rpm` for what a person waits on (fetch_package, whatprovides, resolve, test_connection), answered from
snapshots younger than RPM_FRESH_SECONDS without asking the source; `rpm-batch` for the daily work (detect_changes,
sync_repo), which refreshes them. A source argument is the registry as the API sends it (`id`, `slug`, `base_url`,
`auth_type`, `credentials_ref`, `config` with `namespace`, `arch`, `releasever`, `modular`).
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
                result_expires=3600, worker_prefetch_multiplier=1, task_acks_late=True,
                task_routes={"rpm.detect_changes": {"queue": "rpm-batch"}, "rpm.sync_repo": {"queue": "rpm-batch"}})


def fingerprint(builds: List[Mapping[str, Any]]) -> str:
    """A package's revision in a source: its builds (NEVRA and checksum), whatever their order."""
    keys = sorted("%s-%s.%s %s" % (b["name"], b["evr"], b["arch"], (b.get("checksum") or {}).get("value", ""))
                  for b in builds)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _snapshots(source: repo.Source, refresh: bool = False) -> Dict[str, repo.Snapshot]:
    client = http.make_client(auth_type=source.auth_type, credentials_ref=source.credentials_ref)
    try:
        return {arch: repo.snapshot(source, arch, client, refresh=refresh) for arch in source.arches}
    finally:
        client.close()


class _Index:
    """Builds by name over a source's snapshots: one pool per arch for the whole task."""

    def __init__(self, source: repo.Source, snapshots: Mapping[str, repo.Snapshot]) -> None:
        from rpm_worker import pools
        self.by_name: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for arch, snap in snapshots.items():
            pool = pools.cached_pool(arch, [(source.slug, snap.solv)])
            for solvable in pool.solvables_iter():
                if solvable.arch in ("src", "nosrc") or solvable.repo.name != source.slug:
                    continue
                # noarch builds show up under every arch: one entry per NEVRA, with the arches whose repo lists it.
                key = "%s-%s.%s" % (solvable.name, solvable.evr, solvable.arch)
                builds = self.by_name.setdefault(solvable.name, {})
                if key not in builds:
                    builds[key] = dict(pools.describe(solvable), repo_arches=[])
                if arch not in builds[key]["repo_arches"]:
                    builds[key]["repo_arches"].append(arch)

    def builds(self, name: str) -> List[Dict[str, Any]]:
        return sorted(self.by_name.get(name, {}).values(), key=lambda b: (b["arch"], b["evr"]))


@app.task(name="rpm.sync_repo", time_limit=1800, soft_time_limit=1700)
def sync_repo(source: Mapping[str, Any]) -> Dict[str, Any]:
    src = repo.source_from(source)
    snapshots = _snapshots(src, refresh=True)
    return {"revisions": {arch: s.revision for arch, s in snapshots.items()}}


@app.task(name="rpm.fetch_package", time_limit=600, soft_time_limit=560)
def fetch_package(source: Mapping[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """The builds of a package in the source; None when the source's repodata, read, does not have it."""
    src = repo.source_from(source)
    snapshots = _snapshots(src)
    builds = _Index(src, snapshots).builds(name)
    if not builds:
        return None
    from rpm_worker import modules
    return {"builds": builds, "revision": fingerprint(builds),
            "modules": modules.artifact_streams(snapshots.values(), builds)}


@app.task(name="rpm.detect_changes", time_limit=3600, soft_time_limit=3500)
def detect_changes(source: Mapping[str, Any], tracked: Mapping[str, Optional[str]],
                   known_revisions: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """For `{name: stored revision}`: the names whose builds changed (with the new revision) and those gone. When
    every arch's repository is unchanged since `known_revisions` (and every package has a revision), nothing is
    compared (§8.1)."""
    src = repo.source_from(source)
    snapshots = _snapshots(src, refresh=True)
    revisions = {arch: s.revision for arch, s in snapshots.items()}
    if known_revisions and dict(known_revisions) == revisions and all(tracked.values()):
        return {"changed": {}, "missing": [], "revisions": revisions, "skipped": True}
    index = _Index(src, snapshots)
    changed: Dict[str, str] = {}
    missing: List[str] = []
    for name, known in tracked.items():
        builds = index.builds(name)
        if not builds:
            missing.append(name)
            continue
        now = fingerprint(builds)
        if now != known:
            changed[name] = now
    return {"changed": changed, "missing": missing, "revisions": revisions, "skipped": False}


@app.task(name="rpm.test_connection", time_limit=120)
def test_connection(source: Mapping[str, Any]) -> Dict[str, Any]:
    return {"revision": repo.revision(repo.source_from(source))}


@app.task(name="rpm.resolve", time_limit=900, soft_time_limit=870)
def resolve(request: Mapping[str, Any]) -> Dict[str, Any]:
    from rpm_worker import solver
    return solver.resolve(request)


@app.task(name="rpm.whatprovides", time_limit=300, soft_time_limit=280)
def whatprovides(sources: List[Mapping[str, Any]], arch: str, capability: str) -> List[Dict[str, Any]]:
    """The packages providing a capability (a name, a file, `name = evr`, a rich dependency), newest first."""
    from rpm_worker import pools
    pairs = []
    for raw in sources:
        src = repo.source_from(raw)
        if arch in src.arches:
            pairs.append((src.slug, repo.snapshot(src, arch).solv))
    return pools.whatprovides(pools.cached_pool(arch, pairs), capability)
