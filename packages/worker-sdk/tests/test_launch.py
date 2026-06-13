import asyncio

from sluice_worker.launch import Launcher


async def _settle():
    for _ in range(8):
        await asyncio.sleep(0)


class P:
    """Fake worker process: wait() blocks until finish()/terminate()."""

    def __init__(self):
        self._done = asyncio.Event()
        self._code = 0
        self.returncode = None
        self.terminated = False

    async def wait(self):
        await self._done.wait()
        return self._code

    def finish(self, code=0):
        self._code = code
        self.returncode = code
        self._done.set()

    def terminate(self):
        self.terminated = True
        if not self._done.is_set():
            self._code = -15
            self.returncode = -15
            self._done.set()


async def test_starts_each_only_after_previous_is_ready():
    started: list[int] = []
    readied: list[int] = []
    gates = [asyncio.Event() for _ in range(3)]
    procs: list[P] = []

    async def spawn(i):
        started.append(i)
        p = P()
        procs.append(p)
        return p

    async def wait_ready(_proc, i):
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
        p.finish(0)  # all loaded; clean drain
    assert await task == 0


async def test_all_clean_exit_returns_zero():
    procs: list[P] = []

    async def spawn(_i):
        p = P()
        procs.append(p)
        return p

    async def wait_ready(_proc, _i):
        return None

    task = asyncio.create_task(Launcher(instances=2, spawn=spawn, wait_ready=wait_ready).run())
    await _settle()
    for p in procs:
        p.finish(0)
    assert await task == 0


async def test_child_crash_terminates_siblings_and_propagates_code():
    procs: list[P] = []

    async def spawn(_i):
        p = P()
        procs.append(p)
        return p

    async def wait_ready(_proc, _i):
        return None

    task = asyncio.create_task(Launcher(instances=3, spawn=spawn, wait_ready=wait_ready).run())
    await _settle()  # all 3 spawned + supervising
    procs[1].finish(7)  # one replica crashes nonzero
    assert await task == 7  # the unit exits nonzero (-> pod/VM restart)
    assert procs[0].terminated and procs[2].terminated  # siblings torn down


async def test_spawn_failure_mid_loop_tears_down_already_started():
    procs: list[P] = []

    async def spawn(i):
        if i == 1:
            raise RuntimeError("spawn boom")  # the spawn itself fails for the 2nd replica
        p = P()
        procs.append(p)
        return p

    async def wait_ready(_proc, _i):
        return None

    task = asyncio.create_task(Launcher(instances=3, spawn=spawn, wait_ready=wait_ready).run())
    try:
        await task
        raise AssertionError("expected the spawn failure to propagate")
    except RuntimeError:
        pass
    assert len(procs) == 1 and procs[0].terminated  # the one started child was torn down


async def test_child_death_before_ready_tears_down_and_raises():
    procs: list[P] = []

    async def spawn(_i):
        p = P()
        procs.append(p)
        return p

    async def wait_ready(proc, i):
        if i == 0:
            raise RuntimeError("worker 0 exited before ready")  # what _wait_marker raises on early death
        return None

    task = asyncio.create_task(Launcher(instances=3, spawn=spawn, wait_ready=wait_ready).run())
    try:
        await task
        raise AssertionError("expected the launcher to propagate the startup failure")
    except RuntimeError:
        pass
    assert len(procs) == 1 and procs[0].terminated  # only child 0 spawned, and it was torn down
