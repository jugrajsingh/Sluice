from __future__ import annotations

import asyncio
import importlib
import os

from .broker_client import BrokerClient
from .config import WorkerSettings
from .worker import Worker


def load_handler(path: str):
    module, _, cls = path.partition(":")
    return getattr(importlib.import_module(module), cls)


async def _amain() -> None:
    ws = WorkerSettings()
    handler_cls = load_handler(os.environ["WORKER__HANDLER"])
    broker = BrokerClient(base_url=ws.broker_url, token=ws.broker_token)
    await Worker(broker=broker, handler=handler_cls(), settings=ws).run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
