from __future__ import annotations

import base64
import json
import time

from ..errors import KeyNotFound
from ..interfaces import ObjectStore


class ObjectStoreCache:
    """Cache over any ObjectStore. Expiry is stamped in the object; eviction is lazy."""

    def __init__(self, *, store: ObjectStore, root: str = "sluice/cache") -> None:
        self._store = store
        self._root = root

    def _key(self, key: str) -> str:
        return f"{self._root}/{key}"

    async def get(self, key: str) -> bytes | None:
        try:
            raw = await self._store.get(self._key(key))
        except KeyNotFound:
            return None
        doc = json.loads(raw)
        if time.time() >= doc["expires_at"]:
            await self._store.delete(self._key(key))
            return None
        return base64.b64decode(doc["b64"])

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        doc = {"expires_at": time.time() + ttl_s, "b64": base64.b64encode(value).decode()}
        await self._store.put(self._key(key), json.dumps(doc).encode(), content_type="application/json")

    async def delete(self, key: str) -> None:
        await self._store.delete(self._key(key))
