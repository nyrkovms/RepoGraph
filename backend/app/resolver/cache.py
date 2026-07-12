import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from app.config import get_settings


class ResolveCache:
    CACHE_SCHEMA_VERSION = "v3"

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self.settings = get_settings()
        self.ttl_seconds = ttl_seconds
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(self.settings.redis_url, decode_responses=True)
        return self._client

    @staticmethod
    def build_key(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"resolve:{ResolveCache.CACHE_SCHEMA_VERSION}:{digest}"

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            value = await self.client.get(key)
            if not value:
                return None
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return None

    async def set(self, key: str, payload: dict[str, Any]) -> None:
        try:
            await self.client.setex(key, self.ttl_seconds, json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
        except Exception:  # noqa: BLE001
            return
