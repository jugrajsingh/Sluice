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
        instances: int,
        linger_s: int,
        env: dict[str, str],
        runner: Runner,
        worker_type: str = "handler",
        args: list[str] | None = None,
        root: str = "sluice",
        poll_s: float = 15.0,
    ) -> None:
        self._store = store
        self._app = app
        self._vm = vm_id
        self._image = worker_image
        self._instances = instances
        self._worker_type = worker_type
        self._args = list(args or [])
        self._linger = linger_s
        self._env = env
        self._run_cmd = runner
        self._root = root
        self._poll = poll_s
        self._idle_since: float | None = None

    def _docker_run(
        self, name: str, env: dict[str, str], *, gpus: bool, host_net: bool, image_cmd: list[str]
    ) -> list[str]:
        cmd = ["docker", "run", "-d", "--rm"]
        if gpus:
            cmd += ["--gpus", "all"]
        if host_net:
            cmd += ["--network", "host"]  # so the adapter reaches the model server at localhost
        cmd += ["--name", name]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        return [*cmd, self._image, *image_cmd]

    async def _count(self, name: str) -> int:
        _rc, out = await self._run_cmd(["docker", "ps", "-q", "--filter", f"name={name}"])
        return len([line for line in out.splitlines() if line.strip()])

    async def start_workers(self) -> None:
        if self._worker_type == "sidecar":
            if await self._count("sluice-server") == 0:
                # the model server owns the GPU and runs its own entrypoint; it holds NO broker creds
                server_env = {k: v for k, v in self._env.items() if not k.startswith("WORKER__BROKER")}
                await self._run_cmd(
                    self._docker_run("sluice-server", server_env, gpus=True, host_net=True, image_cmd=self._args)
                )
            await self._run_cmd(
                self._docker_run(
                    "sluice-worker",
                    self._env,
                    gpus=False,
                    host_net=True,
                    image_cmd=["python", "-m", "sluice_worker.adapter"],
                )
            )
        else:  # handler: one launcher container packs N replicas (sequential start) on the GPU
            await self._run_cmd(
                self._docker_run(
                    "sluice-worker",
                    self._env,
                    gpus=True,
                    host_net=False,
                    image_cmd=["python", "-m", "sluice_worker.launch", "--instances", str(self._instances)],
                )
            )
        self._idle_since = None

    async def _running(self) -> int:
        # The leasing container (launcher / adapter) is what "busy" means; a warm model server doesn't count.
        return await self._count("sluice-worker")

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
            await self._heartbeat("running", self._instances)  # the unit packs `instances` replicas
            return True
        if command == "start_workers":
            await self.start_workers()
            await self._heartbeat("running", self._instances)
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


def _worker_env(environ: dict[str, str]) -> dict[str, str]:
    """Env forwarded verbatim to worker containers.

    Prefer the explicit `SLUICE_WORKER_ENV` (JSON) the autoscaler sets, so model env that has no
    Sluice prefix — `MODEL__*`, `SERVER__*`, `HF_HUB_OFFLINE`, `LD_LIBRARY_PATH` — survives. Fall
    back to the legacy Sluice-prefix filter when it's absent (so old VMs keep working).
    """
    explicit = environ.get("SLUICE_WORKER_ENV")
    if explicit:
        return json.loads(explicit)
    return {k: v for k, v in environ.items() if k.startswith(("QUEUE__", "OBJECT_STORE__", "WORKER__"))}


async def _subprocess_runner(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode()


def main() -> None:
    import os

    from sluice_core.config import Settings
    from sluice_drivers.factory import build_object_store

    agent = VmAgent(
        store=build_object_store(Settings()),
        app=os.environ["SLUICE_APP"],
        vm_id=os.environ["VM_ID"],
        worker_image=os.environ["WORKER_IMAGE"],
        instances=int(os.environ.get("SLUICE_INSTANCES", os.environ.get("WORKERS_PER_VM", "1"))),
        worker_type=os.environ.get("SLUICE_WORKER_TYPE", "handler"),
        args=json.loads(os.environ.get("SLUICE_ARGS", "[]")),
        linger_s=int(os.environ.get("LINGER_SECONDS", "300")),
        env=_worker_env(dict(os.environ)),
        runner=_subprocess_runner,
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
