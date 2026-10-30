"""One request per interval to an upstream that sets a rate limit (crates.io API: 1 request/s), across every process.

The turn is a Redis key set with NX and the interval as its expiry: whoever sets it may send, the others wait for it
to expire. If Redis cannot be reached the wait falls back to this process alone. Replayed responses (offline tests)
reach no upstream and do not wait.
"""

import asyncio
import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings

logger = logging.getLogger(__name__)

PREFIX = "rate-limit"
_local_next: dict[str, float] = {}


async def _local_turn(key: str, interval_s: float) -> None:
    # The slot is taken before the first await, so no lock is needed (and none is bound to one event loop).
    now = time.monotonic()
    slot = max(now, _local_next.get(key, 0.0))
    _local_next[key] = slot + interval_s
    if slot > now:
        await asyncio.sleep(slot - now)


async def wait_turn(key: str, interval_s: float) -> None:
    settings = get_settings()
    if settings.http_replay_dir:
        return
    client = Redis.from_url(settings.redis_url)
    try:
        while not await client.set(f"{PREFIX}:{key}", b"1", nx=True, px=max(1, round(interval_s * 1000))):
            remaining_ms = await client.pttl(f"{PREFIX}:{key}")
            await asyncio.sleep(max(remaining_ms, 10) / 1000)
    except (RedisError, OSError):
        logger.warning("rate limit %s: Redis unavailable, limiting this process only", key, exc_info=True)
        await _local_turn(key, interval_s)
    finally:
        await client.aclose()
