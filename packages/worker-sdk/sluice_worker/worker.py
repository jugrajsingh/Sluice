from __future__ import annotations

import asyncio
import signal

from .broker_client import TokenExpired
from .config import WorkerSettings
from .handler import BaseHandler


class Worker:
    """Leases work from the gateway broker, runs the handler, writes results via
    signed URLs, and acks — holding only a short-lived JWT (no queue/store creds)."""

    def __init__(self, *, broker, handler: BaseHandler, settings: WorkerSettings) -> None:
        self._broker = broker
        self._handler = handler
        self._cfg = settings
        self._stop = False
        self._processed = 0
        self._inflight: set[str] = set()

    def request_stop(self) -> None:
        self._stop = True

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

    async def _heartbeat(self) -> None:
        while not self._stop:
            await asyncio.sleep(self._cfg.heartbeat_s)
            if self._inflight:
                try:
                    await self._broker.extend(list(self._inflight))
                except Exception:  # noqa: BLE001 - heartbeat is best-effort; a lost beat just lets the lease lapse
                    pass

    async def run(self) -> None:
        self._install_signals()
        await self._handler.load()
        hb = asyncio.create_task(self._heartbeat())
        cfg = self._cfg
        blank = 0
        try:
            while not self._stop and blank < cfg.max_blank_retries and self._processed < cfg.max_jobs:
                items = await self._broker.lease(cfg.batch_size)
                if not items:
                    blank += 1
                    continue
                blank = 0
                self._inflight = {it["lease_id"] for it in items}
                bodies = [await self._broker.get(it["body_url"]) for it in items]
                outputs = await self._handler.predict(bodies)
                for it, out in zip(items, outputs, strict=True):
                    await self._broker.put(it["result_url"], out)
                    await self._broker.ack(it["lease_id"])
                    self._inflight.discard(it["lease_id"])
                    self._processed += 1
        except TokenExpired:
            return  # graceful exit; the autoscaler respawns with a fresh token if work remains
        finally:
            self._stop = True
            hb.cancel()
            await self._broker.aclose()
