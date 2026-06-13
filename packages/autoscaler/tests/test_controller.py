import json

from sluice_autoscaler.controller import Controller, PodManager
from sluice_autoscaler.vm_commands import VmCommander
from sluice_core.drivers.cache_objectstore import ObjectStoreCache
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.errors import ProvisionFailure
from sluice_core.models import (
    AppSpec,
    NodePoolSpec,
    PlacementSpec,
    ProvisionError,
    QueueDepth,
    ResourcesSpec,
    ScalingSpec,
    VmPlacementSpec,
    VmRecord,
    VmState,
    WorkerState,
    WorkerStatus,
)
from sluice_core.testing.fakes import FakeObjectStore
from sluice_core.vm_paths import desired_key, heartbeat_key


def _app(mode="both"):
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_worker=10, scale_up_count=3),
        placement=PlacementSpec(
            mode=mode,
            pricing=["spot"],
            kubernetes=[NodePoolSpec(pricing="spot", selector={"s": "1"}, zones=["z1"])],
            vm=VmPlacementSpec(provider="gce", machine_type="g2", regions=["r1", "r2"], workers_per_vm=2, max_vms=3),
        ),
    )


class FakeQueue:
    def __init__(self, visible):
        self._v = visible

    async def depth(self, source):
        return QueueDepth(visible=self._v)


class FakeInspector:
    def __init__(self, workers=()):
        self._w = list(workers)

    async def workers(self, app):
        return self._w


class FakePods(PodManager):
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_pods(self, app, n, *, selector, candidate_key=""):
        self.created.append((n, candidate_key))

    async def delete_pods(self, app, names):
        self.deleted += names


class FakeCompute:
    def __init__(self, fail_regions=()):
        self.fail = set(fail_regions)
        self.provisioned = []
        self.destroyed = []
        self.records = []

    async def provision(self, app, *, region, pricing, count):
        if region in self.fail:
            raise ProvisionFailure(ProvisionError.STOCKOUT, "ZONE_RESOURCE_POOL_EXHAUSTED")
        self.provisioned.append((region, pricing, count))
        return []

    async def instance_states(self, app):
        return self.records

    async def destroy(self, app, vm_ids):
        self.destroyed += vm_ids


def _controller(tmp_path, *, compute=None, inspector=None, visible=100, cache=None):
    store = FakeObjectStore()
    reg = ObjectStoreAppRegistry(store=store)
    ctl = Controller(
        registry=reg,
        queue=FakeQueue(visible),
        inspector=inspector or FakeInspector(),
        pods=FakePods(),
        compute=compute,
        commander=VmCommander(store=store),
        cache=cache or ObjectStoreCache(store=store),
        store=store,
    )
    return ctl, reg, store


async def test_scale_up_creates_pods_with_candidate_annotation(tmp_path):
    ctl, reg, _ = _controller(tmp_path)
    await ctl.reconcile_one(_app())
    assert ctl._pods.created == [(3, "kubernetes/k8s/z1/l4/spot")]
    assert (await reg.get_status("m")).phase == "Scaling"


async def test_stuck_pod_reaped_and_marked(tmp_path):
    stuck = [
        WorkerStatus(
            pod="p",
            state=WorkerState.unschedulable,
            age_s=999,
            reason="ZONE_RESOURCE_POOL_EXHAUSTED",
            candidate="kubernetes/k8s/z1/l4/spot",
        )
    ]
    cache = ObjectStoreCache(store=FakeObjectStore())
    ctl, _reg, _ = _controller(tmp_path, inspector=FakeInspector(stuck), cache=cache)
    await ctl.reconcile_one(_app())
    assert "p" in ctl._pods.deleted
    assert await cache.get("stockout/kubernetes/k8s/z1/l4/spot") is not None


async def test_k8s_exhausted_provisions_vm(tmp_path):
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set("stockout/kubernetes/k8s/z1/l4/spot", b"out", ttl_s=600)
    compute = FakeCompute()
    ctl, reg, _ = _controller(tmp_path, compute=compute, cache=cache)
    await ctl.reconcile_one(_app())
    assert compute.provisioned == [("r1", "spot", 3)]  # ceil(10/2)=5 capped maxVms=3
    assert (await reg.get_status("m")).candidate == "vm/gce/r1/l4/spot"


async def test_provision_stockout_marks_shared_cache_then_next_region(tmp_path):
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set("stockout/kubernetes/k8s/z1/l4/spot", b"out", ttl_s=600)
    compute = FakeCompute(fail_regions={"r1"})
    ctl, _reg, _ = _controller(tmp_path, compute=compute, cache=cache)
    await ctl.reconcile_one(_app())
    assert await cache.get("stockout/vm/gce/r1/l4/spot") is not None
    ctl._cooldown_until["m"] = 0.0  # bypass cooldown for the retry
    await ctl.reconcile_one(_app())
    assert ("r2", "spot", 3) in compute.provisioned


async def test_idle_vm_gets_warm_restart_command(tmp_path):
    compute = FakeCompute()
    compute.records = [
        VmRecord(
            id="v1",
            app="m",
            provider="gce",
            region="r1",
            pricing="spot",
            machine_type="g2",
            state=VmState.running,
            created_at=0,
        )
    ]
    ctl, _reg, store = _controller(tmp_path, compute=compute, visible=5)
    await store.put(heartbeat_key("m", "v1"), json.dumps({"phase": "workers_exited", "workers": 0, "ts": 0}).encode())
    await ctl.reconcile_one(_app())
    cmd = json.loads(await store.get(desired_key("m", "v1")))
    assert cmd["action"] == "start_workers"


async def test_preempted_vm_destroyed(tmp_path):
    compute = FakeCompute()
    compute.records = [
        VmRecord(
            id="v1",
            app="m",
            provider="gce",
            region="r1",
            pricing="spot",
            machine_type="g2",
            state=VmState.preempted,
            created_at=0,
        )
    ]
    ctl, _reg, _ = _controller(tmp_path, compute=compute, visible=0)
    await ctl.reconcile_one(_app())
    assert compute.destroyed == ["v1"]
