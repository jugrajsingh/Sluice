from __future__ import annotations

import httpx


class TokenExpired(Exception):
    """The broker rejected the worker token (401) — it is expired or revoked."""


class BrokerClient:
    """HTTP client for the gateway broker. The worker holds only a short-lived JWT;
    it never touches the queue or the object store directly."""

    def __init__(self, *, base_url: str, token: str, timeout_s: float = 30.0, transport=None) -> None:
        # Authed client for the gateway broker (control endpoints + blob proxy).
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            transport=transport,
        )
        # Bare client for absolute pre-signed object-store URLs — the worker JWT must NOT
        # be sent to S3/GCS (they reject requests carrying both a query signature and an
        # Authorization header).
        self._bare = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    def _io_client(self, url: str) -> httpx.AsyncClient:
        return self._bare if url.startswith(("http://", "https://")) else self._http

    async def _post(self, path: str, json: dict) -> dict:
        resp = await self._http.post(path, json=json)
        if resp.status_code == 401:
            raise TokenExpired(resp.text)
        resp.raise_for_status()
        return resp.json()

    async def lease(self, max: int) -> list[dict]:
        return (await self._post("/internal/v1/lease", {"max": max}))["items"]

    async def extend(self, lease_ids: list[str]) -> None:
        await self._post("/internal/v1/extend", {"lease_ids": lease_ids})

    async def ack(self, lease_id: str) -> None:
        await self._post("/internal/v1/ack", {"lease_id": lease_id})

    async def nack(self, lease_id: str) -> None:
        await self._post("/internal/v1/nack", {"lease_id": lease_id})

    async def get(self, url: str) -> bytes:
        resp = await self._io_client(url).get(url)
        resp.raise_for_status()
        return resp.content

    async def put(self, url: str, data: bytes) -> None:
        resp = await self._io_client(url).put(url, content=data)
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._bare.aclose()
