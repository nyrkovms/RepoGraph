import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.pypi import PyPIConnector
from app.fetcher.pipeline import find_package, run_sync_pipeline
from app.normalizers.names import normalize_package_name

logger = logging.getLogger(__name__)


class LazyFetcher:
    def __init__(self, connector: PyPIConnector, db: AsyncSession):
        self.connector = connector
        self.db = db
        self._in_flight: dict[str, asyncio.Event] = {}

    async def ensure_package(self, name: str):
        normalized = normalize_package_name(name)

        package = await find_package(self.db, "pypi", normalized)
        if package:
            return package

        if normalized in self._in_flight:
            await self._in_flight[normalized].wait()
            return await find_package(self.db, "pypi", normalized)

        event = asyncio.Event()
        self._in_flight[normalized] = event
        try:
            package_data = await self.connector.fetch_package_metadata(normalized)
            return await run_sync_pipeline(package_data, self.db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lazy fetch failed for %s: %s", normalized, exc)
            return None
        finally:
            event.set()
            self._in_flight.pop(normalized, None)
