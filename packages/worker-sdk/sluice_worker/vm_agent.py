"""VM agent: supervises worker containers on a burst VM; the gateway broker is the only channel.

Runs as a container with the docker socket mounted. When run() returns, the
container exits and the host startup script powers the VM off.

The agent holds NO object-store credentials (ADR-002/008): it reports its heartbeat and receives
commands through the gateway broker via a :class:`VmStateChannel`, exactly as the workers reach the
queue and objects only through the broker. This keeps a GCE VM working against an S3 store (and vice
versa), where the VM's ambient/attached credentials are invalid for the storage cloud.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]

logger = logging.getLogger(__name__)


class VmStateChannel(Protocol):
    """How the VM agent reports state and receives commands — the broker in production, a store-backed
    double in tests. The VM holds no store credentials; all state I/O goes through this channel."""

    async def heartbeat(self, phase: str, workers: int) -> None: ...

    async def pop_command(self) -> str | None: ...


class _BrokerVmChannel:
    """VmStateChannel over the gateway broker (the VM holds only its short-lived JWT)."""

    def __init__(self, broker: Any, vm_id: str) -> None:
        self._broker = broker
        self._vm = vm_id

    async def heartbeat(self, phase: str, workers: int) -> None:
        await self._broker.vm_heartbeat(self._vm, phase, workers)

    async def pop_command(self) -> str | None:
        return await self._broker.vm_command(self._vm)


class VmAgent:
    def __init__(
        self,
        *,
        channel: VmStateChannel,
        app: str,
        vm_id: str,
        worker_image: str,
        instances: int,
        linger_s: int,
        env: dict[str, str],
        runner: Runner,
        app_image: str | None = None,
        gpu: bool = True,
        worker_type: str = "handler",
        args: list[str] | None = None,
        poll_s: float = 15.0,
    ) -> None:
        self._channel = channel
        self._app = app
        self._vm = vm_id
        # worker_image (the Sluice worker-base) runs the agent itself and the sidecar adapter — they
        # need sluice_worker. app_image (the BYO model image) runs the sidecar server / handler
        # launcher; it stays a pure model image (no sluice_worker). Defaults to worker_image so a
        # combined handler image still works without an explicit app_image.
        self._image = worker_image
        self._app_image = app_image or worker_image
        self._gpu = gpu
        self._instances = instances
        self._worker_type = worker_type
        self._args = list(args or [])
        self._linger = linger_s
        self._env = env
        self._run_cmd = runner
        self._poll = poll_s
        self._idle_since: float | None = None

    def _docker_run(
        self, name: str, env: dict[str, str], *, image: str, gpus: bool, host_net: bool, image_cmd: list[str]
    ) -> list[str]:
        cmd = ["docker", "run", "-d", "--rm"]
        if gpus:
            cmd += ["--gpus", "all"]
        if host_net:
            cmd += ["--network", "host"]  # so the adapter reaches the model server at localhost
        cmd += ["--name", name]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        return [*cmd, image, *image_cmd]

    async def _count(self, name: str) -> int:
        rc, out = await self._run_cmd(["docker", "ps", "-q", "--filter", f"name={name}"])
        if rc != 0:
            # The query itself failed (e.g. the agent can't reach the docker socket). Count nothing —
            # never mistake an error message for a running container, or we'd report phase=running
            # forever while launching nothing.
            logger.warning("docker ps for %s failed (rc=%s): %s", name, rc, out.strip()[:300])
            return 0
        return len([line for line in out.splitlines() if line.strip()])

    async def _run_container(
        self, name: str, env: dict[str, str], *, image: str, gpus: bool, host_net: bool, image_cmd: list[str]
    ) -> None:
        # Clear any stale container of this name first (e.g. a crashed/stopped one left after a
        # warm restart) so `docker run --name` doesn't collide.
        await self._run_cmd(["docker", "rm", "-f", name])
        rc, out = await self._run_cmd(
            self._docker_run(name, env, image=image, gpus=gpus, host_net=host_net, image_cmd=image_cmd)
        )
        if rc != 0:
            logger.warning("docker run %s (%s) failed (rc=%s): %s", name, image, rc, out.strip()[:500])
        else:
            logger.info("launched %s from %s", name, image)

    async def start_workers(self) -> None:
        if self._worker_type == "sidecar":
            if await self._count("sluice-server") == 0:
                # the BYO model image, UNCHANGED: its own entrypoint, owns the GPU, holds NO broker creds
                server_env = {k: v for k, v in self._env.items() if not k.startswith("WORKER__BROKER")}
                await self._run_container(
                    "sluice-server",
                    server_env,
                    image=self._app_image,
                    gpus=self._gpu,
                    host_net=True,
                    image_cmd=self._args,
                )
            # the adapter runs in the Sluice worker-base image (it needs sluice_worker), holds the JWT,
            # and reaches the server over localhost; never a GPU.
            await self._run_container(
                "sluice-worker",
                self._env,
                image=self._image,
                gpus=False,
                host_net=True,
                image_cmd=["python", "-m", "sluice_worker.adapter"],
            )
        else:  # handler: one launcher container (the Sluice-handler image) packs N replicas on the GPU
            await self._run_container(
                "sluice-worker",
                self._env,
                image=self._app_image,
                gpus=self._gpu,
                host_net=False,
                image_cmd=["python", "-m", "sluice_worker.launch", "--instances", str(self._instances)],
            )
        self._idle_since = None

    async def _running(self) -> int:
        # The leasing container (launcher / adapter) is what "busy" means; a warm model server doesn't count.
        return await self._count("sluice-worker")

    async def _heartbeat(self, phase: str, workers: int) -> None:
        await self._channel.heartbeat(phase, workers)

    async def _pop_command(self) -> str | None:
        return await self._channel.pop_command()

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
            restarted = await self._running()  # confirm the container actually came up (no name collision/failure)
            await self._heartbeat("running" if restarted else "workers_exited", self._instances if restarted else 0)
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

    from .broker_client import BrokerClient
    from .config import WorkerSettings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ws = WorkerSettings()
    vm_id = os.environ["VM_ID"]
    # The agent reaches the broker with the same short-lived JWT the workers use (WORKER__BROKER_*,
    # already in the VM env) — no object-store credentials anywhere on the VM.
    broker = BrokerClient(base_url=ws.broker_url, token=ws.broker_token)
    agent = VmAgent(
        channel=_BrokerVmChannel(broker, vm_id),
        app=os.environ["SLUICE_APP"],
        vm_id=vm_id,
        worker_image=os.environ["WORKER_IMAGE"],
        app_image=os.environ.get("APP_IMAGE") or os.environ["WORKER_IMAGE"],
        gpu=os.environ.get("SLUICE_GPU", "1") == "1",
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
