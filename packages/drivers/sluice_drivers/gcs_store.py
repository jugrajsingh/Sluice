from __future__ import annotations

import asyncio
import os

import aiohttp
from gcloud.aio.storage import Storage
from sluice_core.errors import KeyNotFound


class GcsObjectStore:
    """ObjectStore over Google Cloud Storage."""

    def __init__(self, *, bucket: str, endpoint: str | None = None) -> None:
        self._bucket = bucket
        self._endpoint = endpoint
        if endpoint:  # fake-gcs-server / emulator
            os.environ["STORAGE_EMULATOR_HOST"] = endpoint
        self._storage = Storage()

    async def ensure_bucket(self) -> None:
        # Real GCS buckets are created out-of-band; against an emulator
        # (fake-gcs-server) we must create the bucket explicitly — it does not
        # auto-create on first write. Tolerate "already exists" (409).
        if not self._endpoint:
            return
        url = f"{self._endpoint}/storage/v1/b?project=sluice-test"
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json={"name": self._bucket}) as resp:
                        if resp.status not in (200, 409):
                            resp.raise_for_status()
                return
            except aiohttp.ClientConnectionError as e:  # emulator still warming up
                last_exc = e
                await asyncio.sleep(0.2 * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        await self._storage.upload(self._bucket, key, data, content_type=content_type or "application/octet-stream")

    async def get(self, key: str) -> bytes:
        try:
            return await self._storage.download(self._bucket, key)
        except Exception as e:  # gcloud-aio raises on 404
            if "404" in str(e):
                raise KeyNotFound(key) from e
            raise

    async def exists(self, key: str) -> bool:
        try:
            await self._storage.download_metadata(self._bucket, key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        try:
            await self._storage.delete(self._bucket, key)
        except Exception as e:
            if "404" not in str(e):
                raise

    async def signed_url(self, key: str, *, expires_s: int) -> str:
        return f"https://storage.googleapis.com/{self._bucket}/{key}"

    async def list_keys(self, prefix: str) -> list[str]:
        resp = await self._storage.list_objects(self._bucket, params={"prefix": prefix})
        return sorted(item["name"] for item in resp.get("items", []))
