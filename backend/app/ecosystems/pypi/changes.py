"""Daily change detection for pypi.org: serials from the root Simple index (SPEC-DAILY-UPDATE.md §1-2)."""

import asyncio
import logging
from collections.abc import Mapping, Sequence

from app.config import get_settings
from app.ecosystems.base import ChangeSet, TrackedPackage
from app.ecosystems.pypi.simple_index import PyPISimpleIndex

logger = logging.getLogger(__name__)


def plan_changes(tracked: Sequence[TrackedPackage], serials: Mapping[str, int]) -> ChangeSet:
    """Changed: the index serial is newer than ours, or the project came back. Missing: absent from the index."""
    changed: list[tuple[TrackedPackage, str | None]] = []
    missing: list[TrackedPackage] = []
    for package in tracked:
        serial = serials.get(package.ref.name)
        if serial is None:
            # Already known to be unavailable: nothing new to report until it comes back.
            if not package.unavailable:
                missing.append(package)
            continue
        known = int(package.revision) if package.revision and package.revision.isdigit() else 0
        if package.unavailable or serial > known:
            changed.append((package, str(serial)))
    return ChangeSet(changed=changed, missing=missing, cursor={})


class WarehouseChangeDetector:
    def __init__(self, index: PyPISimpleIndex | None = None, concurrency: int | None = None) -> None:
        self.index = index or PyPISimpleIndex()
        self.concurrency = concurrency or get_settings().daily_update_concurrency

    async def detect(self, tracked: Sequence[TrackedPackage], cursor: Mapping[str, object]) -> ChangeSet:
        index_serial: int | None
        try:
            index_serial, serials = await self.index.fetch_all_serials()
        except Exception:  # noqa: BLE001
            # The root index is ~10 MB; when it fails, ask project by project (same fallback as before E0).
            logger.exception("root Simple index failed, falling back to per-project serials")
            index_serial, serials = None, await self._serials_per_project(tracked)
        result = plan_changes(tracked, serials)
        result.cursor = {**cursor, "index_serial": index_serial}
        return result

    async def _serials_per_project(self, tracked: Sequence[TrackedPackage]) -> dict[str, int]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(name: str) -> tuple[str, int | None]:
            async with semaphore:
                return name, await self.index.fetch_project_serial(name)

        pairs = await asyncio.gather(*(one(package.ref.name) for package in tracked))
        return {name: serial for name, serial in pairs if serial is not None}
