"""VM agent: supervises worker containers on a burst VM; bucket is the only channel.

Runs as a container with the docker socket mounted. When run() returns, the
container exits and the host startup script powers the VM off.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from sluice_core.errors import KeyNotFound
from sluice_core.interfaces import ObjectStore
from sluice_core.vm_paths import desired_key, heartbeat_key

Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


class VmAgent:
    def __init__(
        self,
        *,
        store: ObjectStore,
        app: str,
        vm_id: str,
        worker_image: str,
        workers: int,
        linger_s: int,
        env: dict[str, str],
        runner: Runner,
        root: str = "sluice",
        poll_s: float = 15.0,
    ) -> None:
        self._store = store
        self._app = app
        self._vm = vm_id
        self._image = worker_image
        self._workers = workers
        self._linger = linger_s
        self._env = env
        self._run_cmd = runner
        self._root = root
        self._poll = poll_s
        self._idle_since: float | None = None

    async def start_workers(self) -> None:
        for i in range(self._workers):
            env_flags: list[str] = []
            for k, v in self._env.items():
                env_flags += ["-e", f"{k}={v}"]
            await self._run_cmd(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--gpus",
                    "all",
                    "--name",
                    f"sluice-worker-{i}",
                    *env_flags,
                    self._image,
                    "python",
                    "-m",
                    "sluice_worker.run",
                ]
            )
        self._idle_since = None

    async def _running(self) -> int:
        _rc, out = await self._run_cmd(["docker", "ps", "-q", "--filter", "name=sluice-worker-"])
        return len([line for line in out.splitlines() if line.strip()])

    async def _heartbeat(self, phase: str, workers: int) -> None:
        doc = {"phase": phase, "workers": workers, "ts": time.time()}
        await self._store.put(
            heartbeat_key(self._app, self._vm, root=self._root),
            json.dumps(doc).encode(),
            content_type="application/json",
        )

    async def _pop_command(self) -> str | None:
        key = desired_key(self._app, self._vm, root=self._root)
        try:
            raw = await self._store.get(key)
        except KeyNotFound:
            return None
        await self._store.delete(key)
        return json.loads(raw).get("action")

    async def step(self, *, now: float) -> bool:
        """One supervision tick. Returns False when the agent should exit (VM powers off)."""
        command = await self._pop_command()
        if command == "shutdown":
            await self._heartbeat("stopping", 0)
            return False
        running = await self._running()
        if running > 0:
            self._idle_since = None
            await self._heartbeat("running", running)
            return True
        if command == "start_workers":
            await self.start_workers()
            await self._heartbeat("running", self._workers)
            return True
        if self._idle_since is None:
            self._idle_since = now
        if now - self._idle_since >= self._linger:
            await self._heartbeat("stopping", 0)
            return False
        await self._heartbeat("workers_exited", 0)
        return True

    async def run(self) -> None:
        await self._heartbeat("installing", 0)
        await self.start_workers()
        # Periodic supervision tick, not an event wait — sleep-loop is intended.
        while await self.step(now=time.monotonic()):  # noqa: ASYNC110
            await asyncio.sleep(self._poll)


async def _subprocess_runner(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode()


def main() -> None:
    import os

    from sluice_core.config import Settings
    from sluice_drivers.factory import build_object_store

    env_passthrough = {k: v for k, v in os.environ.items() if k.startswith(("QUEUE__", "OBJECT_STORE__", "WORKER__"))}
    agent = VmAgent(
        store=build_object_store(Settings()),
        app=os.environ["SLUICE_APP"],
        vm_id=os.environ["VM_ID"],
        worker_image=os.environ["WORKER_IMAGE"],
        workers=int(os.environ.get("WORKERS_PER_VM", "1")),
        linger_s=int(os.environ.get("LINGER_SECONDS", "300")),
        env=env_passthrough,
        runner=_subprocess_runner,
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
