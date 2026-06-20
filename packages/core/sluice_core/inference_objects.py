from __future__ import annotations

from .interfaces import ObjectStore


class ObjectStoreInferenceObjects:
    """Request/result bodies over any ObjectStore.

    Paths: {prefix}/requests/{request_id} (raw) and {prefix}/results/{request_id}.gz (gzipped),
    where prefix = prefix_template.format(app=app).
    """

    def __init__(self, *, store: ObjectStore, prefix_template: str = "AppData/{app}") -> None:
        self._store = store
        self._tpl = prefix_template

    def _key(self, app: str, kind: str, request_id: str) -> str:
        return f"{self._tpl.format(app=app)}/{kind}/{request_id}"

    async def put_request(self, app: str, request_id: str, body: bytes) -> None:
        await self._store.put(self._key(app, "requests", request_id), body)

    async def get_request(self, app: str, request_id: str) -> bytes:
        return await self._store.get(self._key(app, "requests", request_id))

    async def put_result(self, app: str, request_id: str, body: bytes) -> None:
        await self._store.put(self.result_key(app, request_id), body)

    async def get_result(self, app: str, request_id: str) -> bytes:
        return await self._store.get(self.result_key(app, request_id))

    async def result_exists(self, app: str, request_id: str) -> bool:
        return await self._store.exists(self.result_key(app, request_id))

    def request_key(self, app: str, request_id: str) -> str:
        return self._key(app, "requests", request_id)

    def result_key(self, app: str, request_id: str) -> str:
        # Results are always gzipped (workers gzip before the presigned PUT), so the stored key
        # carries a .gz suffix — a bucket reader knows it's gzipped without out-of-band metadata.
        return f"{self._key(app, 'results', request_id)}.gz"

    async def signed_get_request(self, app: str, request_id: str, *, expires_s: int) -> str:
        return await self._store.signed_url(self.request_key(app, request_id), method="GET", expires_s=expires_s)

    async def signed_put_result(self, app: str, request_id: str, *, expires_s: int) -> str:
        return await self._store.signed_url(self.result_key(app, request_id), method="PUT", expires_s=expires_s)
