import json

from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.vm_paths import desired_key, heartbeat_key
from sluice_worker.vm_agent import VmAgent


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.running = 0

    async def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["docker", "run"]:
            self.running += 1
            return 0, ""
        if args[:2] == ["docker", "ps"]:
            return 0, "\n".join(f"c{i}" for i in range(self.running))
        return 0, ""


def _agent(store, docker, linger=100):
    return VmAgent(
        store=store,
        app="m",
        vm_id="v1",
        worker_image="img",
        workers=2,
        linger_s=linger,
        env={"QUEUE__BACKEND": "memory"},
        runner=docker,
    )


async def test_start_writes_running_heartbeat(tmp_path):
    store, docker = LocalObjectStore(root=str(tmp_path)), FakeDocker()
    agent = _agent(store, docker)
    await agent.start_workers()
    assert await agent.step(now=0.0) is True
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "running" and hb["workers"] == 2
    assert sum(1 for c in docker.calls if c[:2] == ["docker", "run"]) == 2


async def test_idle_then_linger_then_exit(tmp_path):
    store, docker = LocalObjectStore(root=str(tmp_path)), FakeDocker()
    agent = _agent(store, docker, linger=100)
    assert await agent.step(now=0.0) is True  # workers exited (0 running)
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "workers_exited"
    assert await agent.step(now=50.0) is True  # still lingering
    assert await agent.step(now=101.0) is False  # linger expired -> exit (host powers off)
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "stopping"


async def test_warm_restart_on_command(tmp_path):
    store, docker = LocalObjectStore(root=str(tmp_path)), FakeDocker()
    agent = _agent(store, docker)
    assert await agent.step(now=0.0) is True  # idle
    await store.put(desired_key("m", "v1"), json.dumps({"action": "start_workers"}).encode())
    assert await agent.step(now=10.0) is True
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "running"
    assert not await store.exists(desired_key("m", "v1"))  # command consumed


async def test_shutdown_command_exits(tmp_path):
    store, docker = LocalObjectStore(root=str(tmp_path)), FakeDocker()
    agent = _agent(store, docker)
    await store.put(desired_key("m", "v1"), json.dumps({"action": "shutdown"}).encode())
    assert await agent.step(now=0.0) is False
