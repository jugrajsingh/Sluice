"""Handler packing: launch N `worker.run` processes on one GPU, started **sequentially**.

Each child loads its own model copy into the shared GPU; starting them one at a time (waiting for
each to signal "loaded" before the next) bounds peak host RAM to a single load — the parallel-load
OOM that SamServe solves with a file lock, solved here by ordering. Each child leases independently;
the unit counts as one worker-unit (tune messagesPerWorker for the packed throughput).

If any replica crashes (nonzero exit), the launcher tears the rest down and exits nonzero so the
pod/VM is restarted as a whole — a half-dead unit that silently serves at reduced capacity is worse.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

Spawn = Callable[[int], Awaitable]  # (index) -> process (async .wait() -> int, .terminate(), .returncode)
WaitReady = Callable[[object, int], Awaitable[None]]  # (process, index) -> None; raises if it dies first


def _terminate(proc) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()


class Launcher:
    def __init__(self, *, instances: int, spawn: Spawn, wait_ready: WaitReady) -> None:
        self._instances = max(instances, 1)
        self._spawn = spawn
        self._wait_ready = wait_ready

    async def run(self) -> int:
        """Start all replicas sequentially, then supervise. Returns the unit exit code (0 = clean
        drain of every replica; nonzero = a replica crashed and the rest were torn down)."""
        procs: list = []
        for i in range(self._instances):
            proc = await self._spawn(i)
            procs.append(proc)
            try:
                await self._wait_ready(proc, i)  # blocks until child i loaded; raises if it dies first
            except Exception:
                for p in procs:
                    _terminate(p)
                raise

        pending = {asyncio.create_task(p.wait()) for p in procs}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                rc = task.result()
                if rc != 0:  # a replica crashed -> take the whole unit down for a clean restart
                    for p in procs:
                        _terminate(p)
                    await asyncio.gather(*pending, return_exceptions=True)
                    return rc
        return 0  # every replica exited cleanly (normal queue-drained shutdown)


def _marker_path(i: int) -> Path:
    return Path(f"/tmp/sluice-worker-{i}.ready")


async def _spawn_subprocess(i: int):
    marker = _marker_path(i)
    marker.unlink(missing_ok=True)
    env = {**os.environ, "WORKER__READY_MARKER": str(marker)}
    return await asyncio.create_subprocess_exec(sys.executable, "-m", "sluice_worker.run", env=env)


def _wait_marker(timeout_s: int = 600, poll_s: float = 1.0) -> WaitReady:
    async def wait_ready(proc, i: int) -> None:
        marker = _marker_path(i)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if marker.exists():
                return
            if proc.returncode is not None:  # child died before signaling ready — fail fast
                raise RuntimeError(f"worker {i} exited ({proc.returncode}) before becoming ready")
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
    raise SystemExit(asyncio.run(launcher.run()))


if __name__ == "__main__":
    main()
