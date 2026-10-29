import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.package import Package


class ResolveCache:
    """Answers by request hash. An answer remembers the packages it read and their latest synced_at (the catalog
    revision); it is served only while that revision still holds, so a catalog change invalidates it."""

    CACHE_SCHEMA_VERSION = "v4"

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
            await self.client.set(key, json.dumps(payload, separators=(",", ":"), ensure_ascii=True), ex=self.ttl_seconds)
        except Exception:  # noqa: BLE001
            return

    async def get_valid(self, db: AsyncSession, key: str) -> dict[str, Any] | None:
        entry = await self.get(key)
        if not entry or "response" not in entry:
            return None
        packages = [uuid.UUID(value) for value in entry.get("packages", [])]
        if packages:
            count, revision = (
                await db.execute(select(func.count(Package.id), func.max(Package.synced_at)).where(Package.id.in_(packages)))
            ).one()
            if count != len(packages) or (revision.isoformat() if revision else None) != entry.get("revision"):
                return None
        return entry["response"]

    async def set_checked(self, key: str, response: dict[str, Any], synced_at: dict[uuid.UUID, datetime]) -> None:
        revision = max(synced_at.values(), default=None)
        await self.set(key, {
            "packages": sorted(str(package_id) for package_id in synced_at),
            "revision": revision.isoformat() if revision else None,
            "response": response,
        })
