from __future__ import annotations

from sluice_core.interfaces import Cache


class StockoutBoard:
    """TTL'd stockout marks shared across all apps (keys are app-independent candidate keys)."""

    def __init__(self, *, cache: Cache, ttl_s: int = 600) -> None:
        self._cache = cache
        self._ttl = ttl_s

    async def mark(self, key: str, reason: str) -> None:
        await self._cache.set(f"stockout/{key}", reason.encode(), ttl_s=self._ttl)

    async def view(self, keys: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k in keys:
            v = await self._cache.get(f"stockout/{k}")
            if v is not None:
                out[k] = v.decode()
        return out
