import asyncio

from app.resolver.service import _lazy_fetch_async


def test_lazy_fetch_survives_consecutive_event_loops():
    # The resolver thread calls asyncio.run per missing package (#6): a connection pooled by one loop must not be
    # handed to the next one, which used to end in "attached to a different loop" or a segfault in asyncpg.
    assert asyncio.run(_lazy_fetch_async("tzdata")) == "fetched"
    assert asyncio.run(_lazy_fetch_async("idna")) == "fetched"
    assert asyncio.run(_lazy_fetch_async("tzdata")) == "fetched"
