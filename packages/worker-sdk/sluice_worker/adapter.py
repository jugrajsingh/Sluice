"""Sidecar adapter: feed a packed HTTP model server (SamServe-style) from the broker.

The model server runs in the same unit (pod/VM) and packs its own replicas on the GPU. This
adapter holds the short-lived JWT and does the queue work the server doesn't know about: lease ->
fetch body via signed URL -> POST the body verbatim to the local server -> store the response
verbatim -> ack. Verbatim passthrough (ADR/spec): the queue body is the request payload and the
response is the result; the adapter is model-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import httpx


class Adapter:
    def __init__(
        self,
        *,
        broker,
        server: httpx.AsyncClient,
        request_path: str = "/",
        method: str = "POST",
        content_type: str = "application/octet-stream",
        health_path: str = "/healthz",
        ready_timeout_s: int = 600,
        concurrency: int = 1,
        max_jobs: int = 0,
        max_blank_retries: int = 3,
        poll_s: float = 2.0,
    ) -> None:
        self._broker = broker
        self._server = server
        self._request_path = request_path
        self._method = method
        self._content_type = content_type
        self._health_path = health_path
        self._ready_timeout_s = ready_timeout_s
        self._concurrency = max(concurrency, 1)
        self._max_jobs = max_jobs
        self._max_blank_retries = max_blank_retries
        self._poll_s = poll_s
        self._stop = False

    async def wait_ready(self) -> bool:
        """Block until the local model server answers 200 on health_path, or time out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout_s
        while loop.time() < deadline:
            try:
                resp = await self._server.get(self._health_path)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(self._poll_s)
        raise TimeoutError(f"model server not ready within {self._ready_timeout_s}s")

    async def _safe_nack(self, lease_id: str) -> None:
        with contextlib.suppress(Exception):  # best effort; a lost nack just lets the lease lapse
            await self._broker.nack(lease_id)

    async def _handle(self, item: dict) -> None:
        # The dispatch engine never sees exceptions from here (that would leak the lease); each path
        # nacks at most once (a failing nack must not trigger a second).
        lease_id = item["lease_id"]
        try:
            body = await self._broker.get(item["body_url"])
            resp = await self._server.request(
                self._method, self._request_path, content=body, headers={"content-type": self._content_type}
            )
        except Exception:
            await self._safe_nack(lease_id)
            return
        if 200 <= resp.status_code < 300:
            try:
                await self._broker.put(item["result_url"], resp.content)
                await self._broker.ack(lease_id)
            except Exception:
                await self._safe_nack(lease_id)
        else:
            await self._safe_nack(lease_id)

    def request_stop(self) -> None:
        self._stop = True

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

    async def run(self) -> int:
        from .dispatch import run_dispatch

        self._install_signals()
        return await run_dispatch(
            lease=self._broker.lease,
            handle=self._handle,
            concurrency=self._concurrency,
            should_stop=lambda: self._stop,
            max_jobs=self._max_jobs,
            max_blank_retries=self._max_blank_retries,
        )


async def _amain() -> None:
    from .broker_client import BrokerClient
    from .config import WorkerSettings

    ws = WorkerSettings()
    broker = BrokerClient(base_url=ws.broker_url, token=ws.broker_token)
    server = httpx.AsyncClient(base_url=f"http://localhost:{ws.server_port}", timeout=300.0)
    adapter = Adapter(
        broker=broker,
        server=server,
        request_path=ws.server_request_path,
        method=ws.server_method,
        content_type=ws.server_content_type,
        health_path=ws.server_health_path,
        ready_timeout_s=ws.server_ready_timeout_s,
        concurrency=ws.concurrency,
        max_jobs=ws.max_jobs,
        max_blank_retries=ws.max_blank_retries,
    )
    try:
        await adapter.wait_ready()
        await adapter.run()
    finally:
        await broker.aclose()
        await server.aclose()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
