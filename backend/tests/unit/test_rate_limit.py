"""The shared upstream rate limit (crates.io API, IMPL G3) on a fake Redis."""

import asyncio
import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import get_settings
from app.services import rate_limit


class _FakeRedis:
    """SET NX PX and PTTL over one shared clock, as several processes would see one Redis."""

    keys: dict[str, float] = {}

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def set(self, key: str, value: bytes, nx: bool, px: int) -> bool:
        if self.fail:
            raise RedisConnectionError("down")
        now = time.monotonic()
        if self.keys.get(key, 0.0) > now:
            return False
        self.keys[key] = now + px / 1000
        return True

    async def pttl(self, key: str) -> int:
        return max(0, round((self.keys.get(key, 0.0) - time.monotonic()) * 1000))

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.delenv("HTTP_REPLAY_DIR", raising=False)
    get_settings.cache_clear()
    _FakeRedis.keys = {}
    rate_limit._local_next.clear()
    yield
    get_settings.cache_clear()


def _times(count: int, interval: float) -> list[float]:
    async def scenario() -> list[float]:
        stamps: list[float] = []

        async def one() -> None:
            await rate_limit.wait_turn("crates-io", interval)
            stamps.append(time.monotonic())

        await asyncio.gather(*(one() for _ in range(count)))
        return sorted(stamps)

    return asyncio.run(scenario())


def test_turns_are_spaced_by_the_interval(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.Redis, "from_url", staticmethod(lambda url: _FakeRedis()))
    stamps = _times(3, 0.05)
    assert all(b - a >= 0.04 for a, b in zip(stamps, stamps[1:]))


def test_without_redis_the_process_limits_itself(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.Redis, "from_url", staticmethod(lambda url: _FakeRedis(fail=True)))
    stamps = _times(3, 0.05)
    assert all(b - a >= 0.04 for a, b in zip(stamps, stamps[1:]))


def test_replayed_responses_do_not_wait(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HTTP_REPLAY_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(rate_limit.Redis, "from_url", staticmethod(lambda url: pytest.fail("no Redis in replay")))
    start = time.monotonic()
    _times(3, 1.0)
    assert time.monotonic() - start < 0.5
