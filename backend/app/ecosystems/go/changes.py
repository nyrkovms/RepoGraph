"""Go change detection through index.golang.org (SPEC-MULTI-ECOSYSTEM.md §8.1).

`<index_url>?since=<RFC3339>&limit=<n>` lists `{"Path", "Version", "Timestamp"}` for every version proxy.golang.org
served first since then, oldest first. The cursor is the timestamp of the last entry read. Pages are read up to the
time the run started, each filtered by the module paths of the catalog. Pseudo-versions count only for modules without
tags (revision `latest:<version>`): `@v/list` does not show them, so for a tagged module they change nothing. The first run, a cursor the index refuses and the weekly check go through every tracked module with the
list detector given as `reconciler` (without one the run re-reads them all, `reconcile_all`).
"""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from app.ecosystems.base import CHANGED_SINCE, ChangeDetector, ChangeSet, RegistryConfig, TrackedPackage
from app.ecosystems.go.versions import is_pseudo

# A module without tags: its revision is its latest pseudo-version, so the index's pseudo-versions count for it.
TAGLESS = "latest:"
from app.services.http import make_client

logger = logging.getLogger(__name__)

PAGE_SIZE = 2000          # the most the index returns
TIMEOUT_SECONDS = 60.0
MAX_PAGES = 1000          # ~1 page per 10 minutes of the index: a week behind is ~1000 pages
RECONCILE_EVERY = timedelta(days=7)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GoIndexChangesDetector:
    def __init__(self, registry: RegistryConfig, index_url: str, page_size: int = PAGE_SIZE,
                 reconciler: ChangeDetector | None = None) -> None:
        self.registry = registry
        self.index_url = index_url
        self.page_size = page_size
        self.reconciler = reconciler

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        changes = await self._read_index(tracked, cursor)
        now = datetime.now(UTC)
        reconciled = cursor.get("reconciled_at")
        due = changes.reconcile_all or not isinstance(reconciled, str) or \
            datetime.fromisoformat(reconciled) <= now - RECONCILE_EVERY
        changes.cursor = {**changes.cursor, "reconciled_at": now.isoformat() if due else reconciled}
        if due and self.reconciler is not None:
            checked = await self.reconciler.detect(tracked, {})
            changed = {package.package_id: (package, revision) for package, revision in checked.changed}
            for package, revision in changes.changed:
                changed.setdefault(package.package_id, (package, revision))
            changes.changed = list(changed.values())
            changes.missing = list(checked.missing)
            due = False
        changes.reconcile_all = due
        return changes

    async def _read_index(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        started = datetime.now(UTC)
        since = _timestamp(cursor.get("since"))
        if since is None:
            # Never read: nothing to replay from; every module is checked once and the index starts now.
            return ChangeSet(changed=[], missing=[], cursor={"since": started.isoformat()}, reconcile_all=True)
        by_path = {package.ref.name: package for package in tracked}
        changed: dict[str, TrackedPackage] = {}
        pages = 0
        async with make_client(TIMEOUT_SECONDS) as client:
            while since < started and pages < MAX_PAGES:
                try:
                    page = await client.get(self.index_url, params={"since": since.isoformat().replace("+00:00", "Z"),
                                                                    "limit": self.page_size})
                    if page.status_code == 400 and pages == 0:
                        # The index refuses this cursor: start again from now after a full check.
                        return ChangeSet(changed=[], missing=[], cursor={"since": started.isoformat()},
                                         reconcile_all=True)
                    page.raise_for_status()
                    entries = [json.loads(line) for line in page.text.splitlines() if line.strip()]
                except (httpx.HTTPError, ValueError):
                    if pages == 0:
                        raise
                    # Keep what the pages read so far gave: the next run resumes from the last good page.
                    logger.warning("Go index for %s failed after %s pages at %s", self.registry.slug, pages, since,
                                   exc_info=True)
                    break
                pages += 1
                last = since
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    stamp = _timestamp(entry.get("Timestamp"))
                    if stamp is not None and stamp > last:
                        last = stamp
                    version = entry.get("Version")
                    package = by_path.get(str(entry.get("Path")))
                    if package is None or not isinstance(version, str):
                        continue
                    if not is_pseudo(version) or (package.revision or "").startswith(TAGLESS):
                        changed[package.package_id] = package
                if len(entries) < self.page_size or last <= since:
                    since = max(last, since)
                    break   # the index has nothing newer
                since = last
        # An index entry names a module, not its list: re-read it, and treat a list that still has the stored
        # revision as a lagging proxy (the pipeline retries it).
        return ChangeSet(
            changed=[(package, f"{CHANGED_SINCE}{package.revision}" if package.revision else None)
                     for package in changed.values()],
            missing=[],
            cursor={"since": since.isoformat()},
        )
