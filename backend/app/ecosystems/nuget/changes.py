"""NuGet change detection through the nuget.org catalog (SPEC-MULTI-ECOSYSTEM.md §8.1).

The catalog index (`config.catalog_url`, ~4.5 MB) lists pages with the `commitTimeStamp` of their newest commit; a page
lists `{"@type": "nuget:PackageDetails" | "nuget:PackageDelete", "nuget:id", "nuget:version", "commitTimeStamp"}`
(~690 entries, ~230 KB; about a dozen pages a day). The cursor is the newest commit read. Pages newer than the cursor
are read up to the start of the run, entries filtered by the catalog's ids (case-insensitive); a delete is re-read like
any change and comes back as a removed version or a 404. The first run, a cursor older than the index can tell
(never: the catalog keeps everything) and the weekly check go through every tracked package with the ETag detector given
as `reconciler` (without one the run re-reads them all, `reconcile_all`).
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from app.ecosystems.base import CHANGED_SINCE, ChangeDetector, ChangeSet, RegistryConfig, TrackedPackage
from app.services.http import make_client

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60.0
MAX_PAGES = 400            # ~a month behind; beyond that the weekly check has long covered it
RECONCILE_EVERY = timedelta(days=7)
_TYPES = ("nuget:PackageDetails", "nuget:PackageDelete")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class NuGetCatalogChangesDetector:
    def __init__(self, registry: RegistryConfig, catalog_url: str, reconciler: ChangeDetector | None = None) -> None:
        self.registry = registry
        self.catalog_url = catalog_url
        self.reconciler = reconciler

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        changes = await self._read_catalog(tracked, cursor)
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

    async def _read_catalog(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        started = datetime.now(UTC)
        since = _timestamp(cursor.get("commit"))
        if since is None:
            # Never read: nothing to replay from; every package is checked once and the catalog starts now.
            return ChangeSet(changed=[], missing=[], cursor={"commit": started.isoformat()}, reconcile_all=True)
        by_id = {package.ref.name.lower(): package for package in tracked}
        changed: dict[str, TrackedPackage] = {}
        last = since
        async with make_client(TIMEOUT_SECONDS) as client:
            index = await client.get(self.catalog_url)
            index.raise_for_status()
            # A commit spanning pages may still be written past the index's own commitTimeStamp: stop there.
            committed = _timestamp(index.json().get("commitTimeStamp"))
            if committed is not None and committed < started:
                started = committed
            pages = sorted(
                ((stamp, str(page["@id"])) for page in index.json().get("items") or []
                 if isinstance(page, dict) and (stamp := _timestamp(page.get("commitTimeStamp"))) is not None
                 and stamp > since and isinstance(page.get("@id"), str)),
            )[:MAX_PAGES]
            for number, (_, url) in enumerate(pages):
                try:
                    page = await client.get(url)
                    page.raise_for_status()
                    entries = page.json().get("items") or []
                except (httpx.HTTPError, ValueError):
                    if number == 0:
                        raise
                    # Keep what the pages read so far gave: the next run resumes from the last good page.
                    logger.warning("NuGet catalog of %s failed after %s pages", self.registry.slug, number,
                                   exc_info=True)
                    break
                page_last = last
                for entry in entries:
                    stamp = _timestamp(entry.get("commitTimeStamp")) if isinstance(entry, dict) else None
                    # Commits after the run started wait for the next run, so the cursor never skips them.
                    if stamp is None or stamp <= since or stamp > started or entry.get("@type") not in _TYPES:
                        continue
                    page_last = max(page_last, stamp)
                    package = by_id.get(str(entry.get("nuget:id", "")).lower())
                    if package is not None:
                        changed[package.package_id] = package
                last = page_last
        # A catalog entry names a package, not its registration: re-read it, and treat a registration that still has
        # the stored ETag as a lagging CDN (the pipeline retries it).
        return ChangeSet(
            changed=[(package, f"{CHANGED_SINCE}{package.revision}" if package.revision else None)
                     for package in changed.values()],
            missing=[],
            cursor={"commit": last.isoformat()},
        )
