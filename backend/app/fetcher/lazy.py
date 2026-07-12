import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import BaseConnector, PackageNotFound
from app.fetcher.pipeline import find_package, run_sync_pipeline
from app.fetcher.transport import RegistryAuthError
from app.normalizers.names import normalize_package_name

logger = logging.getLogger(__name__)

LazyFetchStatus = Literal["ok", "not_found", "auth_error", "upstream_error"]


@dataclass(frozen=True)
class LazyFetchResult:
    """Typed outcome: 'package is unknown upstream' and 'upstream is down'
    are different answers — /resolve fail-fast and probe need to know which."""

    status: LazyFetchStatus
    package: object | None = None
    error: str | None = None


class LazyFetcher:
    def __init__(self, connector: BaseConnector, db: AsyncSession):
        self.connector = connector
        self.db = db
        self._in_flight: dict[str, asyncio.Event] = {}

    async def ensure_package_result(self, name: str) -> LazyFetchResult:
        normalized = normalize_package_name(name)

        package = await find_package(self.db, "pypi", normalized)
        if package:
            return LazyFetchResult(status="ok", package=package)

        if normalized in self._in_flight:
            await self._in_flight[normalized].wait()
            package = await find_package(self.db, "pypi", normalized)
            if package:
                return LazyFetchResult(status="ok", package=package)
            return LazyFetchResult(status="upstream_error", error="concurrent fetch did not produce the package")

        event = asyncio.Event()
        self._in_flight[normalized] = event
        try:
            package_data = await self.connector.fetch_package_metadata(normalized)
            package = await run_sync_pipeline(package_data, self.db)
            return LazyFetchResult(status="ok", package=package)
        except PackageNotFound:
            return LazyFetchResult(status="not_found", error=f"package '{normalized}' not found upstream")
        except RegistryAuthError as exc:
            logger.error("Lazy fetch auth error for %s: %s", normalized, exc)
            return LazyFetchResult(status="auth_error", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lazy fetch failed for %s: %s", normalized, exc)
            return LazyFetchResult(status="upstream_error", error=str(exc))
        finally:
            event.set()
            self._in_flight.pop(normalized, None)

    async def ensure_package(self, name: str):
        """Backward-compatible wrapper: package or None."""
        result = await self.ensure_package_result(name)
        return result.package
