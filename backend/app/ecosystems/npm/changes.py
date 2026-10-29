"""npm change detection through the registry's replication feed (SPEC-MULTI-ECOSYSTEM.md §8.2, Q10).

`<changes_url>` answers `{"update_seq"}`; `<changes_url>_changes?since=<seq>&limit=<n>` lists `{"seq", "id",
"deleted"?}` for every change of the whole registry (~4 per second). The cursor is `last_seq`. Pages are read up to the
`update_seq` taken at the start, each filtered by the names of the catalog; a lost or reset feed asks for a full
conditional check (`reconcile_all`). Documents are not in the feed: changed packages are re-read by the pipeline.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from app.ecosystems.base import CHANGED_SINCE, ChangeSet, PackageRef, RegistryConfig, TrackedPackage
from app.services.http import make_client

logger = logging.getLogger(__name__)

PAGE_SIZE = 10_000
TIMEOUT_SECONDS = 60.0
# A full conditional check once a week guards against changes the feed missed (§8.2, point 6).
RECONCILE_EVERY = timedelta(days=7)


def display_name(ref: PackageRef) -> str:
    return f"@{ref.namespace}/{ref.name}" if ref.namespace else ref.name


class NpmChangesDetector:
    def __init__(self, registry: RegistryConfig, changes_url: str, page_size: int = PAGE_SIZE) -> None:
        self.registry = registry
        self.changes_url = changes_url.rstrip("/") + "/"
        self.page_size = page_size

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        changes = await self._read_feed(tracked, cursor)
        now = datetime.now(UTC)
        reconciled = cursor.get("reconciled_at")
        due = changes.reconcile_all or not isinstance(reconciled, str) or \
            datetime.fromisoformat(reconciled) <= now - RECONCILE_EVERY
        changes.cursor = {**changes.cursor, "reconciled_at": now.isoformat() if due else reconciled}
        changes.reconcile_all = due
        return changes

    async def _read_feed(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        by_name = {display_name(package.ref): package for package in tracked}
        async with make_client(TIMEOUT_SECONDS) as client:
            root = await client.get(self.changes_url)
            root.raise_for_status()
            target = int(root.json()["update_seq"])
            last = cursor.get("last_seq")
            if not isinstance(last, int) or isinstance(last, bool):
                # Never read: nothing to replay from; every package is checked once and the feed starts here.
                return ChangeSet(changed=[], missing=[], cursor={"last_seq": target}, reconcile_all=True)
            if last > target:
                logger.warning("npm feed of %s went back from %s to %s: full check", self.registry.slug, last, target)
                return ChangeSet(changed=[], missing=[], cursor={"last_seq": target}, reconcile_all=True)

            changed: dict[str, TrackedPackage] = {}
            pages = 0
            while last < target:
                try:
                    page = await client.get(f"{self.changes_url}_changes",
                                            params={"since": last, "limit": self.page_size})
                    if page.status_code == 400 and pages == 0:
                        # The feed no longer knows this seq (recreated): full check.
                        return ChangeSet(changed=[], missing=[], cursor={"last_seq": target}, reconcile_all=True)
                    page.raise_for_status()
                    payload = page.json()
                except httpx.HTTPError:
                    if pages == 0:
                        raise
                    # Keep what the pages read so far gave: the next run resumes from the last good page.
                    logger.warning("npm feed of %s failed after %s pages at seq %s", self.registry.slug, pages, last,
                                   exc_info=True)
                    break
                pages += 1
                results = payload.get("results") or []
                for entry in results:
                    package = by_name.get(str(entry.get("id")))
                    if package is not None:
                        changed[package.package_id] = package
                next_seq = payload.get("last_seq")
                if not results or not isinstance(next_seq, int) or next_seq <= last:
                    break   # nothing newer: the feed is behind its own update_seq
                last = next_seq

        # A feed entry names a package, not a document revision: re-read it, and treat an answer that still has the
        # stored ETag as a lagging CDN (the pipeline retries it). Deletions come back as 404 on that re-read.
        return ChangeSet(
            changed=[(package, f"{CHANGED_SINCE}{package.revision}" if package.revision else None)
                     for package in changed.values()],
            missing=[],
            cursor={"last_seq": last},
        )
