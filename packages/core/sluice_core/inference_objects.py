from __future__ import annotations

from .interfaces import ObjectStore


class ObjectStoreInferenceObjects:
    """Request/result bodies over any ObjectStore.

    Paths: {prefix}/requests/{request_id} and {prefix}/results/{request_id},
    where prefix = prefix_template.format(app=app).
    """

    def __init__(self, *, store: ObjectStore, prefix_template: str = "apps/{app}") -> None:
        self._store = store
        self._tpl = prefix_template

    def _key(self, app: str, kind: str, request_id: str) -> str:
        return f"{self._tpl.format(app=app)}/{kind}/{request_id}"

    async def put_request(self, app: str, request_id: str, body: bytes) -> None:
        await self._store.put(self._key(app, "requests", request_id), body)

    async def get_request(self, app: str, request_id: str) -> bytes:
        return await self._store.get(self._key(app, "requests", request_id))

    async def put_result(self, app: str, request_id: str, body: bytes) -> None:
        await self._store.put(self._key(app, "results", request_id), body)

    async def get_result(self, app: str, request_id: str) -> bytes:
        return await self._store.get(self._key(app, "results", request_id))

    async def result_exists(self, app: str, request_id: str) -> bool:
        return await self._store.exists(self._key(app, "results", request_id))
