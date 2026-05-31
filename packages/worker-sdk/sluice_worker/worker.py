from __future__ import annotations

import asyncio
import signal

from sluice_core.interfaces import InferenceObjects, Queue

from .config import WorkerSettings
from .handler import BaseHandler


class Worker:
    def __init__(
        self, *, queue: Queue, objects: InferenceObjects, handler: BaseHandler | None, settings: WorkerSettings
    ) -> None:
        self._q = queue
        self._objects = objects
        self._handler = handler
        self._cfg = settings
        self._stop = False
        self._processed = 0

    def request_stop(self) -> None:
        self._stop = True

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

    async def run(self) -> None:
        self._install_signals()
        await self._handler.load()
        blank = 0
        cfg = self._cfg
        while not self._stop and blank < cfg.max_blank_retries and self._processed < cfg.max_jobs:
            msgs = await self._q.receive(cfg.source, max_messages=cfg.batch_size, wait_seconds=cfg.wait_seconds)
            if not msgs:
                blank += 1
                continue
            blank = 0
            rids = [m.body.decode() for m in msgs]
            batch = [await self._objects.get_request(cfg.app, rid) for rid in rids]
            outputs = await self._handler.predict(batch)
            for msg, rid, out in zip(msgs, rids, outputs, strict=True):
                await self._objects.put_result(cfg.app, rid, out)
                await self._q.ack(cfg.source, msg)
                self._processed += 1
