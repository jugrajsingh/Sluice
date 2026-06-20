import json
import logging
import time

import pytest
from sluice_autoscaler.controller import Controller, PodManager
from sluice_autoscaler.placement import candidate_key, expand_candidates
from sluice_autoscaler.vm_commands import VmCommander
from sluice_core.drivers.cache_objectstore import ObjectStoreCache
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.errors import ProvisionFailure
from sluice_core.models import (
    AppSpec,
    BatchSpec,
    K8sPlacementSpec,
    KubernetesCandidate,
    ProvisionError,
    QueueDepth,
    ResourcesSpec,
    ScalingSpec,
    VmCandidate,
    VmPlacementSpec,
    VmRecord,
    VmState,
    WorkerState,
    WorkerStatus,
)
from sluice_core.testing.fakes import FakeObjectStore
from sluice_core.vm_paths import desired_key, heartbeat_key
from sluice_core.vm_tracker import VmTracker


def _app():
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_instance=10, max_scale_up_per_cycle=3),
        placement=[
            KubernetesCandidate(
                provider="in-cluster", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "1"}])
            ),
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(pricing="spot", machine_type="g2", regions=["r1", "r2"]),
            ),
        ],
    )


def _k8s_key():
    return candidate_key(expand_candidates(_app())[0])


def _vm_key(region):
    return candidate_key(next(c for c in expand_candidates(_app()) if c.type == "vm" and c.location == region))


class FakeQueue:
    """Returns depth per queue-ref when ``depths`` dict is given; falls back to a single
    ``visible`` value for all refs (preserves compatibility with existing tests)."""

    def __init__(self, visible: int = 0, depths: dict[str, int] | None = None) -> None:
        self._v = visible
        self._depths = depths or {}

    async def depth(self, source: str) -> QueueDepth:
        if source in self._depths:
            return QueueDepth(visible=self._depths[source])
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

    async def create_pods(self, app, n, *, selector, candidate_key="", tolerations=None, **kw):
        self.created.append((n, candidate_key))

    async def delete_pods(self, app, names):
        self.deleted += names


class FakeCompute:
    def __init__(self, fail_regions=()):
        self.fail = set(fail_regions)
        self.provisioned = []
        self.destroyed = []
        self.reset_ids = []
        self.records = []

    async def provision(self, app, *, region, pricing, count, **kw):
        if region in self.fail:
            raise ProvisionFailure(ProvisionError.STOCKOUT, "ZONE_RESOURCE_POOL_EXHAUSTED")
        self.provisioned.append((region, pricing, count))
        return [
            VmRecord(
                id=f"sluice-{app.name}-{region}-{i}",
                app=app.name,
                provider="gce",
                region=region,
                zone=f"{region}-a",
                pricing=pricing,
                machine_type="g2",
                state=VmState.provisioning,
                created_at=1.0,
            )
            for i in range(count)
        ]

    async def instance_states(self, app):
        return self.records

    async def delete_instance(self, record):
        # Stateless reap: direct API delete-by-name (the controller resolves the record for its zone).
        self.destroyed.append(record.id)

    async def reset_instance(self, record):
        self.reset_ids.append(record.id)


def _controller(
    tmp_path, *, compute=None, inspector=None, visible=100, cache=None, clusters=None, queue=None, tracker=None
):
    store = FakeObjectStore()
    reg = ObjectStoreAppRegistry(store=store)
    ctl = Controller(
        registry=reg,
        queue=queue or FakeQueue(visible),
        inspector=inspector or FakeInspector(),
        pods=FakePods(),
        clusters=clusters,
        compute=compute,
        commander=VmCommander(store=store),
        cache=cache or ObjectStoreCache(store=store),
        store=store,
        tracker=tracker,
    )
    return ctl, reg, store


async def test_scale_up_creates_pods_with_candidate_annotation(tmp_path):
    ctl, reg, _ = _controller(tmp_path)
    await ctl.reconcile_one(_app())
    assert ctl._pods.created == [(3, _k8s_key())]
    assert (await reg.get_status("m")).phase == "Scaling"


async def test_stuck_pod_reaped_and_marked(tmp_path):
    stuck = [
        WorkerStatus(
            pod="p",
            state=WorkerState.unschedulable,
            age_s=999,
            reason="ZONE_RESOURCE_POOL_EXHAUSTED",
            candidate=_k8s_key(),
        )
    ]
    cache = ObjectStoreCache(store=FakeObjectStore())
    ctl, _reg, _ = _controller(tmp_path, inspector=FakeInspector(stuck), cache=cache)
    await ctl.reconcile_one(_app())
    assert "p" in ctl._pods.deleted
    assert await cache.get(f"stockout/{_k8s_key()}") is not None


async def test_k8s_exhausted_provisions_vm(tmp_path):
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)
    compute = FakeCompute()
    ctl, reg, _ = _controller(tmp_path, compute=compute, cache=cache)
    await ctl.reconcile_one(_app())
    assert compute.provisioned == [("r1", "spot", 3)]  # need 10 units -> capped by maxScaleUpPerCycle=3
    assert (await reg.get_status("m")).candidate == _vm_key("r1")


async def test_provision_stockout_marks_shared_cache_then_next_region(tmp_path):
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)
    compute = FakeCompute(fail_regions={"r1"})
    ctl, _reg, _ = _controller(tmp_path, compute=compute, cache=cache)
    await ctl.reconcile_one(_app())
    assert await cache.get(f"stockout/{_vm_key('r1')}") is not None
    # A stockout launches no VM, so NO cooldown is armed: the very next reconcile fails over to the
    # next region immediately (no wasted debounce cycle). The region's stockout mark (TTL) is what
    # prevents re-hammering it; the cooldown is only a post-scale-up debounce.
    assert ctl._cooldown_until.get("m", 0.0) == 0.0
    await ctl.reconcile_one(_app())
    assert ("r2", "spot", 3) in compute.provisioned


async def test_successful_vm_provision_arms_cooldown(tmp_path):
    # Runaway guard (must NOT regress): a VM that actually launched DOES arm the cooldown, so the next
    # cycle waits for the prober to observe it before considering more capacity (no double-provision on
    # prober lag). k8s candidate is pre-stocked to force the walk onto the VM candidate.
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)
    compute = FakeCompute()
    ctl, _reg, _ = _controller(tmp_path, compute=compute, cache=cache)
    await ctl.reconcile_one(_app())
    assert compute.provisioned  # a VM launched
    assert ctl._cooldown_until.get("m", 0.0) > 0.0  # cooldown armed after a successful launch


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


def _multi_cluster_app():
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_instance=10, max_scale_up_per_cycle=3),
        placement=[
            KubernetesCandidate(
                provider="in-cluster", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "1"}])
            ),
            KubernetesCandidate(
                provider="gke-east", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "2"}])
            ),
        ],
    )


async def test_creates_pods_in_external_cluster_when_local_is_stocked(tmp_path):
    app = _multi_cluster_app()
    local_key = candidate_key(expand_candidates(app)[0])
    east_key = candidate_key(expand_candidates(app)[1])
    east_pods = FakePods()
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{local_key}", b"out", ttl_s=600)  # local cluster exhausted
    ctl, _reg, _ = _controller(tmp_path, cache=cache, clusters={"gke-east": (east_pods, FakeInspector())})
    await ctl.reconcile_one(app)
    assert east_pods.created == [(3, east_key)]  # placed in the external cluster
    assert ctl._pods.created == []  # in-cluster manager untouched


def _app_with_unregistered_first():
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_instance=10, max_scale_up_per_cycle=3),
        placement=[
            KubernetesCandidate(provider="ghost", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "1"}])),
            KubernetesCandidate(
                provider="in-cluster", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "2"}])
            ),
        ],
    )


async def test_unregistered_cluster_candidate_is_skipped_for_registered_fallback(tmp_path):
    app = _app_with_unregistered_first()
    ctl, _reg, _ = _controller(tmp_path)  # only in-cluster registered (no 'ghost')
    await ctl.reconcile_one(app)
    fallback_key = candidate_key(expand_candidates(app)[1])  # the in-cluster candidate
    assert ctl._pods.created == [(3, fallback_key)]  # skipped ghost, placed on the registered fallback


async def test_only_unregistered_cluster_holds_with_reason(tmp_path):
    app = AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_instance=10, max_scale_up_per_cycle=3),
        placement=[
            KubernetesCandidate(provider="ghost", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "1"}]))
        ],
    )
    ctl, reg, _ = _controller(tmp_path)
    await ctl.reconcile_one(app)
    st = await reg.get_status("m")
    assert st.phase == "Held" and "ghost" in (st.reason or "")  # surfaced, not stuck in Scaling
    assert ctl._pods.created == []


async def test_stuck_pod_in_external_cluster_is_deleted_in_that_cluster(tmp_path):
    app = AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_instance=10, max_scale_up_per_cycle=3),
        placement=[
            KubernetesCandidate(provider="gke-east", spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"s": "2"}]))
        ],
    )
    east_key = candidate_key(expand_candidates(app)[0])
    east_pods = FakePods()
    stuck = WorkerStatus(
        pod="ep", state=WorkerState.unschedulable, age_s=999, reason="ZONE_RESOURCE_POOL_EXHAUSTED", candidate=east_key
    )
    cache = ObjectStoreCache(store=FakeObjectStore())
    ctl, _reg, _ = _controller(tmp_path, cache=cache, clusters={"gke-east": (east_pods, FakeInspector([stuck]))})
    await ctl.reconcile_one(app)
    assert "ep" in east_pods.deleted  # reaped in the external cluster, not locally
    assert "ep" not in ctl._pods.deleted
    assert await cache.get(f"stockout/{east_key}") is not None


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


# ---------------------------------------------------------------------------
# VM-tracking ledger (ADR-012)
# ---------------------------------------------------------------------------
async def test_provision_upserts_to_ledger(tmp_path):
    tracker = VmTracker(FakeObjectStore())
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)  # force the vm candidate
    ctl, _reg, _ = _controller(tmp_path, compute=FakeCompute(), cache=cache, tracker=tracker)
    await ctl.reconcile_one(_app())
    names = {e.name for e in await tracker.entries("m", "r1")}
    assert names == {f"sluice-m-r1-{i}" for i in range(3)}  # the 3 provisioned VMs recorded


async def test_reap_marks_gone_in_ledger(tmp_path):
    tracker = VmTracker(FakeObjectStore())
    await tracker.upsert("m", "r1", name="v1", state="running", created_at=1.0)
    compute = FakeCompute()
    compute.records = [
        VmRecord(
            id="v1",
            app="m",
            provider="gce",
            region="r1",
            zone="r1-a",
            pricing="spot",
            machine_type="g2",
            state=VmState.preempted,
            created_at=0,
        )
    ]
    ctl, _reg, _ = _controller(tmp_path, compute=compute, visible=0, tracker=tracker)
    await ctl.reconcile_one(_app())
    assert compute.destroyed == ["v1"]
    assert await tracker.entries("m", "r1") == []  # mark_gone removed the reaped VM


async def test_prober_failure_logs_to_ledger_and_does_not_reap(tmp_path):
    tracker = VmTracker(FakeObjectStore())

    class BoomCompute(FakeCompute):
        async def instance_states(self, app):
            raise RuntimeError("prober down")

    compute = BoomCompute()
    ctl, _reg, _ = _controller(tmp_path, compute=compute, tracker=tracker)
    with pytest.raises(RuntimeError):
        await ctl.reconcile_one(_app())
    assert compute.destroyed == []  # NEVER reap when the prober is unreliable
    assert any("prober failure" in e.error for e in await tracker.events("m", "r1"))


# ---------------------------------------------------------------------------
# Hung-VM detection + escalation (ADR-012, Task 2.5)
# ---------------------------------------------------------------------------
def _running_vm(vm_id="v1", region="r1"):
    return VmRecord(
        id=vm_id,
        app="m",
        provider="gce",
        region=region,
        zone=f"{region}-a",
        pricing="spot",
        machine_type="g2",
        state=VmState.running,
    )


async def _seed_heartbeat(store, vm_id, *, received_at, phase="running", workers=3):
    doc = {"phase": phase, "workers": workers, "received_at": received_at}
    await store.put(heartbeat_key("m", vm_id, root="sluice"), json.dumps(doc).encode())


async def test_unreachable_vm_excluded_so_replacement_provisioned(tmp_path):
    compute = FakeCompute()
    compute.records = [_running_vm("v1")]
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)  # force the vm candidate for the replacement
    ctl, _reg, store = _controller(tmp_path, compute=compute, cache=cache, visible=10)  # need == 1 unit
    await _seed_heartbeat(store, "v1", received_at=time.time() - 300)  # stale > 180s
    await ctl.reconcile_one(_app())
    assert compute.provisioned  # v1 is hung → excluded → a replacement VM is provisioned
    assert "v1" not in compute.destroyed  # not yet at the delete threshold


async def test_fresh_heartbeat_vm_counts_as_serving(tmp_path):
    compute = FakeCompute()
    compute.records = [_running_vm("v1")]
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)
    ctl, _reg, store = _controller(tmp_path, compute=compute, cache=cache, visible=10)  # need == 1 unit
    await _seed_heartbeat(store, "v1", received_at=time.time())  # fresh
    await ctl.reconcile_one(_app())
    assert compute.provisioned == []  # v1 serves the single unit → no replacement (no regression)


async def test_unreachable_vm_reset_then_deleted_by_staleness(tmp_path):
    compute = FakeCompute()
    compute.records = [_running_vm("v1")]
    tracker = VmTracker(FakeObjectStore())
    ctl, _reg, store = _controller(tmp_path, compute=compute, visible=0, tracker=tracker)
    await _seed_heartbeat(store, "v1", received_at=time.time() - 700)  # > reset (600), < delete (1200)
    await ctl.reconcile_one(_app())
    assert compute.reset_ids == ["v1"] and "v1" not in compute.destroyed  # rebooted, not deleted
    await _seed_heartbeat(store, "v1", received_at=time.time() - 1300)  # now > delete (1200)
    await ctl.reconcile_one(_app())
    assert "v1" in compute.destroyed  # reset did not recover it → reaped
    assert await tracker.entries("m", "r1") == []  # mark_gone on the hung delete


async def test_wedged_vm_excluded_after_restart_cap(tmp_path):
    compute = FakeCompute()
    compute.records = [_running_vm("v1")]
    cache = ObjectStoreCache(store=FakeObjectStore())
    await cache.set(f"stockout/{_k8s_key()}", b"out", ttl_s=600)
    ctl, _reg, store = _controller(tmp_path, compute=compute, cache=cache, visible=10)  # need == 1 unit
    # workers keep exiting (fresh heartbeats, so NOT unreachable). Within the cap the VM is warm-restarted
    # and absorbs the unit (no provision); past the cap (>3) it is wedged → excluded → replacement.
    for _ in range(5):
        await _seed_heartbeat(store, "v1", received_at=time.time(), phase="workers_exited")
        await ctl.reconcile_one(_app())
    assert ctl._exited_cycles.get("v1", 0) > _app().scaling.wedged_restart_max
    assert compute.provisioned  # once wedged, a replacement is provisioned instead of endless restarts


async def test_gc_deletes_heartbeat_for_gone_vm(tmp_path):
    compute = FakeCompute()
    compute.records = [_running_vm("v1")]  # the prober reports only v1
    ctl, _reg, store = _controller(tmp_path, compute=compute, visible=0)
    await _seed_heartbeat(store, "v1", received_at=time.time())
    await _seed_heartbeat(store, "ghost", received_at=time.time())  # not in the prober set → gone
    await ctl.reconcile_one(_app())
    assert await store.exists(heartbeat_key("m", "v1", root="sluice"))  # live VM's heartbeat kept
    assert not await store.exists(heartbeat_key("m", "ghost", root="sluice"))  # gone VM's heartbeat GC'd


# ---------------------------------------------------------------------------
# On-change per-app status line (log maturity)
# ---------------------------------------------------------------------------
async def test_status_line_logs_once_then_again_on_change(tmp_path, caplog):
    compute = FakeCompute()
    compute.records = [_running_vm("v1")]  # one healthy VM, steady fleet
    ctl, _reg, store = _controller(tmp_path, compute=compute, visible=0)
    await _seed_heartbeat(store, "v1", received_at=time.time())  # fresh → counts as Healthy

    def _status_lines():
        return [r.message for r in caplog.records if "Healthy:" in r.message]

    with caplog.at_level(logging.INFO, logger="sluice_autoscaler.controller"):
        await ctl.reconcile_one(_app())
        await _seed_heartbeat(store, "v1", received_at=time.time())  # still fresh — identical fleet
        await ctl.reconcile_one(_app())  # second identical cycle must NOT re-log
        assert len(_status_lines()) == 1
        assert "Healthy:1 Starting:0 Draining:0 Unreachable:0" in _status_lines()[0]

        # State change: the VM goes unreachable (stale heartbeat) → the line logs again.
        await _seed_heartbeat(store, "v1", received_at=time.time() - 300)
        await ctl.reconcile_one(_app())
        lines = _status_lines()
        assert len(lines) == 2
        assert "Healthy:0 Starting:0 Draining:0 Unreachable:1" in lines[1]


# ---------------------------------------------------------------------------
# Batch-lane tests (Task 12)
# ---------------------------------------------------------------------------


def _batch_app():
    """VM-only app with a batch block wired up.  rate_per_instance_per_min=10, infer_sla_minutes=5 →
    one instance clears 50 infer items in the SLA window.  batch_sla_hours=24, scaling.maxInstances=2
    is now the hard ceiling fed to sla_desired (the per-app instance cap, not a per-batch one)."""
    return AppSpec(
        name="mb",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(
            messages_per_instance=10,
            max_scale_up_per_cycle=3,
            max_instances=2,
            rate_per_instance_per_min=10,
            infer_sla_minutes=5,
        ),
        placement=[
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(pricing="spot", machine_type="g2", regions=["r1"]),
            ),
        ],
        batch=BatchSpec(batch_sla_hours=24),
    )


async def test_should_provision_for_batch_when_only_batch_queue_has_work(tmp_path):
    """Empty infer queue + N pending batch files → provisions ≥1 VM (capped at scaling.maxInstances=2)."""
    # infer queue empty, batch queue has 5 items
    q = FakeQueue(depths={"mb": 0, "mb-batch": 5})
    compute = FakeCompute()
    ctl, reg, _ = _controller(tmp_path, queue=q, compute=compute)
    await ctl.reconcile_one(_batch_app())
    # sla_desired(infer_visible=0, batch_remaining=5, rate_per_min=10,
    #             infer_sla_min=5, batch_sla_hr=24, max_instances=2)
    # batch_capacity = 10 * 24 * 60 = 14400; batch_need = ceil(5/14400) = 1; capped at 2 → 1
    assert compute.provisioned, "expected at least one VM to be provisioned for batch work"
    total_vms = sum(count for _, _, count in compute.provisioned)
    assert 1 <= total_vms <= 2


async def test_should_scale_by_sla_window_when_infer_backlog_deep(tmp_path):
    """Deep infer queue → provisions per sla_desired, NOT the plain ceil(visible/mpw) formula.

    scaling.maxInstances=2 is the binding cap fed to sla_desired (the per-app instance ceiling).

    rate_per_min=10, infer_sla_min=5 → infer_capacity=50.
    infer_visible=100 → infer_need=ceil(100/50)=2; SLA desired=2.
    Plain formula: ceil(100/10)=10; max_scale_up_per_cycle=3 → would provision 3 first reconcile.
    With SLA formula: desired=2, serving=0, need=2 → provisions min(3,2)=2.
    We assert total VMs provisioned == 2 (SLA formula caps at 2, plain would give 3).
    """
    app = AppSpec(
        name="mb2",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(
            messages_per_instance=10,
            max_scale_up_per_cycle=3,
            max_instances=2,
            rate_per_instance_per_min=10,
            infer_sla_minutes=5,
        ),
        placement=[
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(pricing="spot", machine_type="g2", regions=["r1"]),
            ),
        ],
        batch=BatchSpec(batch_sla_hours=24),
    )
    q = FakeQueue(depths={"mb2-infer": 100, "mb2-batch": 0})
    compute = FakeCompute()
    ctl, _reg, _ = _controller(tmp_path, queue=q, compute=compute)
    await ctl.reconcile_one(app)
    total_vms = sum(count for _, _, count in compute.provisioned)
    # SLA formula: desired=2 → need=2 → provisions min(max_scale_up_per_cycle=3, need=2) = 2
    # Plain formula: desired=10 → need=10 → provisions min(3, 10) = 3  (plain would fail this)
    assert total_vms == 2, f"expected 2 VMs from SLA formula (capped at scaling.maxInstances=2), got {total_vms}"


# ---------------------------------------------------------------------------
# Scale-down stabilization (anti-flap)
# ---------------------------------------------------------------------------
class MutableQueue:
    """A queue whose visible depth can change between reconciles (drives the stabilization test)."""

    def __init__(self, visible: int = 0) -> None:
        self.visible = visible

    async def depth(self, source):  # noqa: ARG002 - same depth for every ref
        return QueueDepth(visible=self.visible)


async def test_scale_down_stabilization_holds_desired_peak_when_queue_drains(tmp_path):
    """A deep queue sets a high desired peak; a transient drain to 0 within the stabilization
    window must NOT immediately collapse desired — the controller holds the held peak as a floor."""
    q = MutableQueue(visible=100)  # cycle 1: ceil(100/10)=10 desired
    ctl, _reg, _ = _controller(tmp_path, queue=q)
    await ctl.reconcile_one(_app())
    peak_value, peak_until = ctl._desired_peak["m"]
    assert peak_value == 10.0  # the peak was armed at the high desired
    import time as _time

    assert peak_until > _time.time()  # the stabilization window is open

    q.visible = 0  # cycle 2: queue drains to empty within the window
    await ctl.reconcile_one(_app())
    # the held peak is still 10 (a transient dip does not shrink it inside the window)
    assert ctl._desired_peak["m"][0] == 10.0


async def test_should_not_destroy_vm_serving_batch_when_infer_idle(tmp_path):
    """A running VM (batch heartbeat active, phase='running') is NOT destroyed when infer is empty.

    Plan accounting: a VM with phase != 'workers_exited' counts as serving_vm_units=1.
    With batch-aware desired=sla_desired(infer=0, batch=5,...)=1 and serving_vm_units=1,
    need=0 → the plan emits no DestroyVm action and returns phase 'Ready'.

    Without the dual-lane observe, the controller computes desired=ceil(0/10)=0 using only
    the infer queue, giving need=0-1=-1 → plan still returns Ready (no destroy), HOWEVER
    the status phase must be 'Ready' (not 'Scaling') so we can verify the correct path.
    We also assert compute.destroyed is empty to guard against any accidental teardown logic.
    """
    q = FakeQueue(depths={"mb": 0, "mb-batch": 5})
    compute = FakeCompute()
    compute.records = [
        VmRecord(
            id="v1",
            app="mb",
            provider="gce",
            region="r1",
            pricing="spot",
            machine_type="g2",
            state=VmState.running,
            created_at=0,
        )
    ]
    store = FakeObjectStore()
    reg = ObjectStoreAppRegistry(store=store)
    ctl = Controller(
        registry=reg,
        queue=q,
        inspector=FakeInspector(),
        pods=FakePods(),
        compute=compute,
        commander=VmCommander(store=store),
        cache=ObjectStoreCache(store=store),
        store=store,
    )
    # Write a heartbeat showing the VM is running (phase="running"), not workers_exited
    await store.put(
        heartbeat_key("mb", "v1"),
        json.dumps({"phase": "running", "workers": 1, "ts": 0}).encode(),
    )
    await ctl.reconcile_one(_batch_app())
    assert "v1" not in compute.destroyed, "VM serving batch must not be destroyed when infer queue is idle"
    st = await reg.get_status("mb")
    # desired=sla_desired(0,5,10,5,24,2)=1, serving_vm_units=1 → need=0 → phase Ready (not Scaling)
    assert st.phase == "Ready", f"expected Ready when VM covers the SLA need, got {st.phase}"
