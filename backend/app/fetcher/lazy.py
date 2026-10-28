import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.ecosystems.base import PackageRef
from app.fetcher.pipeline import find_package, sync_package
from app.models.enums import PackageOrigin

logger = logging.getLogger(__name__)


class LazyFetcher:
    """Brings a package the tree meets into the catalog through the write path (origin=discovered)."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._in_flight: dict[PackageRef, asyncio.Event] = {}

    async def ensure_package(self, ref: PackageRef):
        package = await find_package(self.db, ref)
        if package:
            return package

        if ref in self._in_flight:
            await self._in_flight[ref].wait()
            return await find_package(self.db, ref)

        event = asyncio.Event()
        self._in_flight[ref] = event
        try:
            # A savepoint: a failed fetch rolls back only its own writes. A full rollback would expire every object
            # of the request session (the tree being built) and the next attribute access would fail in async code.
            async with self.db.begin_nested():
                package = await sync_package(self.db, ref, origin=PackageOrigin.discovered)
            await self.db.commit()
            return package
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lazy fetch failed for %s: %s", ref, exc)
            return None
        finally:
            event.set()
            self._in_flight.pop(ref, None)
