import json

from sluice_core.testing.fakes import FakeObjectStore
from sluice_core.vm_objects import VmObjects
from sluice_core.vm_paths import desired_key, heartbeat_key
from sluice_worker.vm_agent import VmAgent, _worker_env


class _StoreChannel:
    """Test double: a VmStateChannel backed by a FakeObjectStore, so the existing store-based
    heartbeat/command assertions still hold while VmAgent talks only to a channel (no store)."""

    def __init__(self, store, app="m", vm_id="v1"):
        self._vo = VmObjects(store)
        self._app = app
        self._vm = vm_id

    async def heartbeat(self, phase, workers):
        await self._vo.put_heartbeat(self._app, self._vm, {"phase": phase, "workers": workers})

    async def pop_command(self):
        return await self._vo.pop_command(self._app, self._vm)


def test_worker_env_prefers_explicit_json():
    # the autoscaler hands the full worker env as SLUICE_WORKER_ENV — non-prefixed keys survive
    env = _worker_env(
        {"SLUICE_WORKER_ENV": json.dumps({"MODEL__VARIANT": "sam3.1", "HF_HUB_OFFLINE": "1"}), "PATH": "/usr/bin"}
    )
    assert env == {"MODEL__VARIANT": "sam3.1", "HF_HUB_OFFLINE": "1"}  # PATH not leaked


def test_worker_env_legacy_prefix_fallback():
    env = _worker_env({"WORKER__APP": "m", "MODEL__X": "y", "PATH": "/x"})
    assert env == {"WORKER__APP": "m"}  # no SLUICE_WORKER_ENV -> only Sluice prefixes


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


def _agent(
    store, docker, linger=100, worker_type="handler", instances=3, args=None, env=None, app_image="appimg", gpu=True
):
    return VmAgent(
        channel=_StoreChannel(store),
        app="m",
        vm_id="v1",
        worker_image="img",  # Sluice worker-base: vm_agent + (sidecar) adapter run here
        app_image=app_image,  # BYO model image: server (sidecar) / launcher (handler)
        gpu=gpu,
        instances=instances,
        worker_type=worker_type,
        args=args,
        linger_s=linger,
        env=env or {"WORKER__BROKER_URL": "http://sluice-gateway"},
        runner=docker,
    )


def _runs(docker):
    return [c for c in docker.calls if c[:2] == ["docker", "run"]]


async def test_handler_runs_one_launcher_packing_instances():
    store, docker = FakeObjectStore(), FakeDocker()
    agent = _agent(store, docker, worker_type="handler", instances=3)
    await agent.start_workers()
    assert await agent.step(now=0.0) is True
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "running" and hb["workers"] == 3  # unit packs 3, reported as the unit's capacity
    runs = _runs(docker)
    assert len(runs) == 1  # one launcher container
    cmd = runs[0]
    assert "appimg" in cmd  # the launcher runs in the BYO (handler) image, not the worker-base
    assert "--gpus" in cmd and "sluice_worker.launch" in cmd and "--instances" in cmd and "3" in cmd


async def test_sidecar_runs_server_plus_adapter_sharing_network():
    store, docker = FakeObjectStore(), FakeDocker()
    env = {
        "WORKER__BROKER_URL": "http://g",
        "WORKER__BROKER_TOKEN": "tok",
        "MODEL__VARIANT": "sam3.1",
    }  # gitleaks:allow (test fixture)
    agent = _agent(store, docker, worker_type="sidecar", instances=3, args=["--xl"], env=env)
    await agent.start_workers()
    runs = _runs(docker)
    assert len(runs) == 2
    server = next(c for c in runs if "sluice-server" in c)
    worker = next(c for c in runs if "sluice-worker" in c)
    # server: the BYO model image, GPU + host network + its own entrypoint args; broker token NOT passed
    assert "appimg" in server and "--gpus" in server and "--network" in server and server[-1] == "--xl"
    assert not any("WORKER__BROKER_TOKEN" in part for part in server)
    assert any("MODEL__VARIANT=sam3.1" == part for part in server)
    # adapter: the Sluice worker-base image (NOT the model image), host network, no GPU, holds the token
    assert "img" in worker and "appimg" not in worker
    assert "--gpus" not in worker
    assert "--network" in worker and worker[-3:] == ["python", "-m", "sluice_worker.adapter"]
    assert any("WORKER__BROKER_TOKEN=tok" == part for part in worker)


async def test_sidecar_cpu_app_omits_gpus():
    store, docker = FakeObjectStore(), FakeDocker()
    agent = _agent(store, docker, worker_type="sidecar", instances=1, args=[], gpu=False)
    await agent.start_workers()
    server = next(c for c in _runs(docker) if "sluice-server" in c)
    assert "--gpus" not in server  # CPU app: the model server is not given a GPU


async def test_handler_cpu_app_omits_gpus():
    store, docker = FakeObjectStore(), FakeDocker()
    agent = _agent(store, docker, worker_type="handler", instances=1, gpu=False)
    await agent.start_workers()
    assert "--gpus" not in _runs(docker)[0]


async def test_idle_then_linger_then_exit():
    store, docker = FakeObjectStore(), FakeDocker()
    agent = _agent(store, docker, linger=100)
    assert await agent.step(now=0.0) is True  # workers exited (0 running)
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "workers_exited"
    assert await agent.step(now=50.0) is True  # still lingering
    assert await agent.step(now=101.0) is False  # linger expired -> exit (host powers off)
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "stopping"


async def test_warm_restart_on_command():
    store, docker = FakeObjectStore(), FakeDocker()
    agent = _agent(store, docker)
    assert await agent.step(now=0.0) is True  # idle
    await store.put(desired_key("m", "v1"), json.dumps({"action": "start_workers"}).encode())
    assert await agent.step(now=10.0) is True
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "running"
    assert not await store.exists(desired_key("m", "v1"))  # command consumed


async def test_start_workers_clears_stale_container_before_run():
    store, docker = FakeObjectStore(), FakeDocker()
    await _agent(store, docker, worker_type="handler", instances=2).start_workers()
    rm_idx = next(i for i, c in enumerate(docker.calls) if c[:3] == ["docker", "rm", "-f"] and "sluice-worker" in c)
    run_idx = next(i for i, c in enumerate(docker.calls) if c[:2] == ["docker", "run"] and "sluice-worker" in c)
    assert rm_idx < run_idx  # stale container removed before the named run -> no --name collision


async def test_warm_restart_failed_run_reports_workers_exited_not_running():
    class FailRunDocker(FakeDocker):
        async def __call__(self, args):
            self.calls.append(args)
            if args[:2] == ["docker", "run"]:
                return 1, "name collision"  # run fails; nothing comes up
            if args[:2] == ["docker", "ps"]:
                return 0, "\n".join(f"c{i}" for i in range(self.running))
            return 0, ""

    store, docker = FakeObjectStore(), FailRunDocker()
    agent = _agent(store, docker)
    await store.put(desired_key("m", "v1"), json.dumps({"action": "start_workers"}).encode())
    assert await agent.step(now=0.0) is True
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "workers_exited"  # failed restart isn't falsely reported as running


async def test_count_ignores_failed_docker_query_so_errors_are_not_workers():
    # If `docker ps` itself fails (e.g. the agent can't reach the docker socket — permission denied),
    # its error text is one non-empty line. It must NOT be counted as a running worker, or the agent
    # reports phase=running forever while it actually launched nothing (a silently no-op burst VM).
    class DeniedDocker(FakeDocker):
        async def __call__(self, args):
            self.calls.append(args)
            if args[:2] == ["docker", "ps"]:
                return 126, "permission denied while trying to connect to the Docker daemon socket"
            return 0, ""

    store, docker = FakeObjectStore(), DeniedDocker()
    agent = _agent(store, docker)
    assert await agent._running() == 0  # the denied-query error line is not a worker
    await agent.step(now=0.0)
    hb = json.loads(await store.get(heartbeat_key("m", "v1")))
    assert hb["phase"] == "workers_exited"


async def test_shutdown_command_exits():
    store, docker = FakeObjectStore(), FakeDocker()
    agent = _agent(store, docker)
    await store.put(desired_key("m", "v1"), json.dumps({"action": "shutdown"}).encode())
    assert await agent.step(now=0.0) is False
