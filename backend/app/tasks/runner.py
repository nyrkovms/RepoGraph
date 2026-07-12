"""asyncio.run wrapper for Celery tasks.

Closes the loop-bound shared HTTP client before the loop dies, so worker
processes do not leak connections across task runs.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.fetcher.transport import aclose_shared_client

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    async def _with_cleanup() -> T:
        try:
            return await coro
        finally:
            await aclose_shared_client()

    return asyncio.run(_with_cleanup())
