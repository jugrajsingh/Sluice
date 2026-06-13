"""Handler packing: launch N `worker.run` processes on one GPU, started **sequentially**.

Each child loads its own model copy into the shared GPU; starting them one at a time (waiting for
each to signal "loaded" before the next) bounds peak host RAM to a single load — the parallel-load
OOM that SamServe solves with a file lock, solved here by ordering. Each child leases independently;
the unit counts as one worker-unit (tune messagesPerWorker for the packed throughput).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

Spawn = Callable[[int], Awaitable]  # (index) -> process (with async .wait())
WaitReady = Callable[[int], Awaitable[None]]


class Launcher:
    def __init__(self, *, instances: int, spawn: Spawn, wait_ready: WaitReady) -> None:
        self._instances = max(instances, 1)
        self._spawn = spawn
        self._wait_ready = wait_ready

    async def run(self) -> int:
        procs = []
        for i in range(self._instances):
            procs.append(await self._spawn(i))
            await self._wait_ready(i)  # block until child i has loaded before starting i+1
        await asyncio.gather(*(p.wait() for p in procs))
        return len(procs)


def _marker_path(i: int) -> Path:
    return Path(f"/tmp/sluice-worker-{i}.ready")


async def _spawn_subprocess(i: int):
    marker = _marker_path(i)
    marker.unlink(missing_ok=True)
    env = {**os.environ, "WORKER__READY_MARKER": str(marker)}
    return await asyncio.create_subprocess_exec(sys.executable, "-m", "sluice_worker.run", env=env)


def _wait_marker(timeout_s: int = 600, poll_s: float = 1.0) -> WaitReady:
    async def wait_ready(i: int) -> None:
        marker = _marker_path(i)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if marker.exists():
                return
            await asyncio.sleep(poll_s)
        raise TimeoutError(f"worker {i} did not become ready within {timeout_s}s")

    return wait_ready


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, default=int(os.environ.get("WORKER__INSTANCES", "1")))
    ap.add_argument("--ready-timeout-s", type=int, default=int(os.environ.get("WORKER__SERVER_READY_TIMEOUT_S", "600")))
    args = ap.parse_args()
    launcher = Launcher(
        instances=args.instances, spawn=_spawn_subprocess, wait_ready=_wait_marker(timeout_s=args.ready_timeout_s)
    )
    asyncio.run(launcher.run())


if __name__ == "__main__":
    main()
