"""Keep-busy concurrent dispatch: hold up to `concurrency` jobs in flight, replenish on completion.

Replaces the batch-then-wait pattern for callers that feed a packed model (the sidecar adapter
keeps ~`instances` requests in flight against an HTTP server with N workers). `handle(item)` does
the full per-item work (fetch body -> infer/POST -> write result -> ack/nack) and must not raise on
business failures — it owns its own nack. The engine only orchestrates concurrency, replenishment,
and the stop conditions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

Lease = Callable[[int], Awaitable[list[Any]]]
Handle = Callable[[Any], Awaitable[None]]


async def run_dispatch(
    *,
    lease: Lease,
    handle: Handle,
    concurrency: int,
    should_stop: Callable[[], bool],
    max_jobs: int = 0,
    max_blank_retries: int = 3,
) -> int:
    """Run until stopped, drained (`max_blank_retries` consecutive idle empty leases), or `max_jobs`.

    Returns the number of completed jobs. `lease(n)` should return at most `n` items (long-polling
    when empty is fine). `concurrency` is the max simultaneous in-flight `handle` tasks.
    """
    inflight: set[asyncio.Task] = set()
    processed = 0
    blank = 0

    while not should_stop() and blank < max_blank_retries and (max_jobs == 0 or processed < max_jobs):
        slots = concurrency - len(inflight)
        if max_jobs:
            slots = min(slots, max_jobs - processed - len(inflight))
        items = await lease(slots) if slots > 0 else []
        if items:
            blank = 0
            inflight.update(asyncio.create_task(handle(it)) for it in items)
        elif not inflight:
            blank += 1
            continue
        if inflight:
            done, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.exception()  # retrieve so a handle that raised doesn't warn; handle owns nack
            processed += len(done)

    if inflight:
        results = await asyncio.gather(*inflight, return_exceptions=True)
        processed += len(results)
    return processed
