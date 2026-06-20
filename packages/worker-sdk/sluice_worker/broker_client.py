from __future__ import annotations

import asyncio

import httpx
from sluice_core.models import Message


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


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
        # Bare client for pre-signed object-store URLs — the worker JWT must NOT be sent to
        # S3/GCS (they reject a request carrying both a query signature and an Authorization
        # header). Object I/O always uses pre-signed URLs, so it always goes through here.
        self._bare = httpx.AsyncClient(timeout=timeout_s, transport=transport)

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

    # --- batch lane (spec §5 / C1) ----------------------------------------
    # The {app}-batch queue is leased through the broker exactly like infer; one
    # message == one JSONL file. batch_lease returns Message objects whose attributes
    # carry job_id/file/body_url and whose ack_token is the broker lease_id, so the
    # adapter's batch lane reads them directly.

    async def batch_lease(self, max: int) -> list[Message]:
        items = (await self._post("/internal/v1/batch/lease", {"max": max}))["items"]
        return [
            Message(
                id=it["lease_id"],
                body=b"",
                ack_token=it["lease_id"],
                attributes={"job_id": it["job_id"], "file": it["file"], "body_url": it["body_url"]},
            )
            for it in items
        ]

    async def batch_ack(self, lease_id: str) -> None:
        await self._post("/internal/v1/batch/ack", {"lease_id": lease_id})

    async def batch_extend(self, lease_ids: list[str]) -> None:
        await self._post("/internal/v1/batch/extend", {"lease_ids": lease_ids})

    async def batch_nack(self, lease_id: str) -> None:
        await self._post("/internal/v1/batch/nack", {"lease_id": lease_id})

    async def batch_output_url(self, job_id: str, file: str, start_offset: int) -> str:
        """Presigned PUT for one output part, minted on demand right before the flush."""
        body = {"job_id": job_id, "file": file, "start_offset": start_offset}
        return (await self._post("/internal/v1/batch/output-url", body))["url"]

    async def batch_status_get(self, job_id: str, file: str) -> dict | None:
        resp = await self._http.get("/internal/v1/batch/status", params={"job_id": job_id, "file": file})
        if resp.status_code == 401:
            raise TokenExpired(resp.text)
        resp.raise_for_status()
        data = resp.json()
        return data["status"] if data.get("found") else None

    async def batch_status_put(self, job_id: str, file: str, status: dict) -> None:
        await self._post("/internal/v1/batch/status", {"job_id": job_id, "file": file, "status": status})

    async def vm_heartbeat(self, vm_id: str, phase: str, workers: int) -> None:
        await self._post("/internal/v1/vm/heartbeat", {"vm_id": vm_id, "phase": phase, "workers": workers})

    async def vm_command(self, vm_id: str) -> str | None:
        resp = await self._http.get("/internal/v1/vm/command", params={"vm_id": vm_id})
        if resp.status_code == 401:
            raise TokenExpired(resp.text)
        resp.raise_for_status()
        return resp.json().get("command")

    async def put_file(self, url: str, path: str) -> None:
        """Upload a spilled output-part file to a presigned PUT URL.

        The file is read off-thread (never blocking the event loop) and sent with a known
        Content-Length, so presigned SigV4 / GCS V4 validation succeeds (a chunked body can fail it).
        """
        data = await asyncio.to_thread(_read_bytes, path)
        resp = await self._bare.put(url, content=data)
        resp.raise_for_status()

    async def get(self, url: str) -> bytes:
        resp = await self._bare.get(url)
        resp.raise_for_status()
        return resp.content

    async def put(self, url: str, data: bytes) -> None:
        resp = await self._bare.put(url, content=data)
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._bare.aclose()
