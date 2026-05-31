from __future__ import annotations


class RedisCache:
    """Cache over Redis (SET ... EX). Redis owns expiry."""

    def __init__(self, *, client) -> None:
        self._r = client

    async def get(self, key: str) -> bytes | None:
        return await self._r.get(key)

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        await self._r.set(key, value, ex=max(ttl_s, 1))

    async def delete(self, key: str) -> None:
        await self._r.delete(key)
