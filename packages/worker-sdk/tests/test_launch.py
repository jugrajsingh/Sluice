import asyncio

from sluice_worker.launch import Launcher


async def _settle():
    for _ in range(6):
        await asyncio.sleep(0)


async def test_starts_each_only_after_previous_is_ready():
    started: list[int] = []
    readied: list[int] = []
    gates = [asyncio.Event() for _ in range(3)]

    class P:
        def __init__(self):
            self._done = asyncio.Event()

        async def wait(self):
            await self._done.wait()
            return 0

        def finish(self):
            self._done.set()

    procs: list[P] = []

    async def spawn(i):
        started.append(i)
        p = P()
        procs.append(p)
        return p

    async def wait_ready(i):
        readied.append(i)
        await gates[i].wait()

    task = asyncio.create_task(Launcher(instances=3, spawn=spawn, wait_ready=wait_ready).run())
    await _settle()
    assert started == [0] and readied == [0]  # blocked on child 0's readiness
    gates[0].set()
    await _settle()
    assert started == [0, 1]  # child 1 starts only after child 0 is ready (no parallel-load OOM)
    gates[1].set()
    await _settle()
    assert started == [0, 1, 2]
    gates[2].set()
    await _settle()
    for p in procs:
        p.finish()  # all loaded; supervise until they exit
    assert await task == 3


async def test_runs_all_instances_and_returns_count():
    class P:
        async def wait(self):
            return 0

    async def spawn(_i):
        return P()

    async def wait_ready(_i):
        return None

    assert await Launcher(instances=4, spawn=spawn, wait_ready=wait_ready).run() == 4
