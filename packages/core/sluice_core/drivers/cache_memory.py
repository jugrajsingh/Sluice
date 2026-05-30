from __future__ import annotations

import asyncio
import time


class MemoryCache:
    """In-process reference Cache with monotonic-clock TTLs."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, deadline = entry
            if time.monotonic() >= deadline:
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        async with self._lock:
            self._data[key] = (value, time.monotonic() + ttl_s)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)
