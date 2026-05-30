"""Reusable Cache conformance tests. Subclass and provide a `cache` fixture."""

from __future__ import annotations

import asyncio

import pytest

from sluice_core.interfaces import Cache


class CacheConformance:
    @pytest.fixture
    def cache(self) -> Cache:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_satisfies_protocol(self, cache: Cache) -> None:
        assert isinstance(cache, Cache)

    async def test_set_get_roundtrip(self, cache: Cache) -> None:
        await cache.set("k", b"v", ttl_s=60)
        assert await cache.get("k") == b"v"

    async def test_missing_returns_none(self, cache: Cache) -> None:
        assert await cache.get("nope") is None

    async def test_overwrite(self, cache: Cache) -> None:
        await cache.set("k2", b"a", ttl_s=60)
        await cache.set("k2", b"b", ttl_s=60)
        assert await cache.get("k2") == b"b"

    async def test_expiry(self, cache: Cache) -> None:
        await cache.set("exp", b"v", ttl_s=1)
        await asyncio.sleep(1.1)
        assert await cache.get("exp") is None

    async def test_delete(self, cache: Cache) -> None:
        await cache.set("d", b"v", ttl_s=60)
        await cache.delete("d")
        await cache.delete("d")  # idempotent
        assert await cache.get("d") is None
