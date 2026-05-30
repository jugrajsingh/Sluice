from sluice_core.models import (
    AppSpec,
    AppStatus,
    NodePoolSpec,
    PlacementSpec,
    VmPlacementSpec,
    WorkerState,
    WorkerStatus,
)


def test_worker_state_values():
    assert WorkerState.unschedulable.value == "unschedulable"
    assert WorkerState("running") is WorkerState.running


def test_worker_status_minimal():
    w = WorkerStatus(pod="p-1", state=WorkerState.pending)
    assert w.reason is None and w.age_s == 0 and w.restarts == 0
    assert w.candidate is None


def test_appspec_defaults_fill_from_name():
    app = AppSpec(name="topwear", image="repo/x:1")
    assert app.queue_ref == "topwear"
    assert app.storage_prefix == "apps/topwear"
    assert app.desired_state == "Ready"
    assert app.scaling.max_workers == 0  # 0 = unbounded
    assert app.scaling.messages_per_worker == 10
    assert app.placement.mode == "kubernetes"
    assert app.placement.pricing == ["spot"]


def test_placement_models():
    p = PlacementSpec(
        mode="both",
        pricing=["spot", "on-demand"],
        kubernetes=[NodePoolSpec(selector={"a": "b"}, zones=["z1"])],
        vm=VmPlacementSpec(provider="gce", machine_type="g2-standard-8", regions=["us-central1", "europe-west3"]),
    )
    assert p.vm.workers_per_vm == 1 and p.vm.linger_seconds == 300 and p.vm.max_vms == 5


def test_appstatus_defaults():
    s = AppStatus()
    assert s.phase == "Ready" and s.reason is None and s.candidate is None
    assert s.workers == {} and s.queue.visible == 0
