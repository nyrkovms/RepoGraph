"""rpm-worker: repodata cache, libsolv pools, modularity and resolve for the RPM ecosystem (SPEC-MULTI-ECOSYSTEM.md §2.2,
§3.2, §9.8). Runs on Rocky Linux 9's Python 3.9, where python3-solv and libmodulemd live; the API reaches it through
Celery tasks on the `rpm` queue."""
