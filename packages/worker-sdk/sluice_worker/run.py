from __future__ import annotations

import asyncio
import importlib
import os

from sluice_core.config import Settings
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_drivers.factory import build_object_store, build_queue

from .config import WorkerSettings
from .worker import Worker


def load_handler(path: str):
    module, _, cls = path.partition(":")
    return getattr(importlib.import_module(module), cls)


async def _amain() -> None:
    s = Settings()
    ws = WorkerSettings()
    handler_cls = load_handler(os.environ["WORKER__HANDLER"])
    worker = Worker(
        queue=build_queue(s),
        objects=ObjectStoreInferenceObjects(store=build_object_store(s)),
        handler=handler_cls(),
        settings=ws,
    )
    await worker.run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
