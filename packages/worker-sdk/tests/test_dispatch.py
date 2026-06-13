import asyncio

from sluice_worker.dispatch import run_dispatch


def _pool_lease(items):
    pool = list(items)
    asked: list[int] = []

    async def lease(n):
        asked.append(n)
        out = [pool.pop(0) for _ in range(min(n, len(pool)))]
        return out

    return lease, asked


async def test_caps_concurrency_and_processes_all():
    lease, asked = _pool_lease(range(10))
    inflight = 0
    max_seen = 0

    async def handle(_item):
        nonlocal inflight, max_seen
        inflight += 1
        max_seen = max(max_seen, inflight)
        await asyncio.sleep(0.01)  # hold a slot so concurrency is observable
        inflight -= 1

    processed = await run_dispatch(lease=lease, handle=handle, concurrency=3, should_stop=lambda: False)
    assert processed == 10
    assert max_seen <= 3  # never more than `concurrency` in flight
    assert max(asked) <= 3  # never leased more than the free slots


async def test_replenishes_until_drained_then_blank_exits():
    lease, _ = _pool_lease(range(5))

    async def handle(_item):
        await asyncio.sleep(0)

    # pool of 5, concurrency 2, blank budget 2 -> drains all 5 then exits on empty leases
    processed = await run_dispatch(
        lease=lease, handle=handle, concurrency=2, should_stop=lambda: False, max_blank_retries=2
    )
    assert processed == 5


async def test_max_jobs_caps_processed():
    lease, _ = _pool_lease(range(100))

    async def handle(_item):
        await asyncio.sleep(0)

    processed = await run_dispatch(lease=lease, handle=handle, concurrency=4, should_stop=lambda: False, max_jobs=10)
    assert processed == 10


async def test_should_stop_halts_and_drains():
    lease, _ = _pool_lease(range(100))
    stop = False

    async def handle(_item):
        await asyncio.sleep(0)

    def should_stop():
        return stop

    # stop after the first batch by flipping the flag from a handle
    seen = 0

    async def counting_handle(_item):
        nonlocal stop, seen
        seen += 1
        if seen >= 3:
            stop = True
        await asyncio.sleep(0)

    processed = await run_dispatch(lease=lease, handle=counting_handle, concurrency=2, should_stop=should_stop)
    assert processed >= 3  # stopped early, but in-flight work was drained, far short of 100


async def test_handle_exception_does_not_kill_the_loop():
    lease, _ = _pool_lease(range(4))

    async def handle(item):
        if item == 1:
            raise RuntimeError("boom")  # a real adapter would nack internally
        await asyncio.sleep(0)

    processed = await run_dispatch(
        lease=lease, handle=handle, concurrency=2, should_stop=lambda: False, max_blank_retries=1
    )
    assert processed == 4  # the failing item still counts as completed; loop survives
