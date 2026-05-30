"""Reusable ObjectStore conformance tests. Subclass and provide a `store` fixture."""

from __future__ import annotations

import pytest

from sluice_core.errors import KeyNotFound
from sluice_core.interfaces import ObjectStore


class ObjectStoreConformance:
    @pytest.fixture
    def store(self) -> ObjectStore:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_satisfies_protocol(self, store: ObjectStore) -> None:
        assert isinstance(store, ObjectStore)

    async def test_put_get_exists(self, store: ObjectStore) -> None:
        await store.put("k1", b"v1")
        assert await store.exists("k1") is True
        assert await store.get("k1") == b"v1"

    async def test_missing_exists_false(self, store: ObjectStore) -> None:
        assert await store.exists("missing") is False

    async def test_get_missing_raises_keynotfound(self, store: ObjectStore) -> None:
        with pytest.raises(KeyNotFound):
            await store.get("missing")

    async def test_delete_is_idempotent(self, store: ObjectStore) -> None:
        await store.put("k2", b"v")
        await store.delete("k2")
        await store.delete("k2")  # second delete must not raise
        assert await store.exists("k2") is False

    async def test_signed_url_nonempty(self, store: ObjectStore) -> None:
        await store.put("k3", b"v")
        assert (await store.signed_url("k3", expires_s=60)) != ""

    async def test_put_overwrites_existing_key(self, store: ObjectStore) -> None:
        await store.put("ow", b"first")
        await store.put("ow", b"second")
        assert await store.get("ow") == b"second"

    async def test_list_keys_filters_by_prefix(self, store: ObjectStore) -> None:
        await store.put("lk/a/1", b"x")
        await store.put("lk/a/2", b"y")
        await store.put("lk/b/3", b"z")
        keys = await store.list_keys("lk/a/")
        assert keys == ["lk/a/1", "lk/a/2"]
