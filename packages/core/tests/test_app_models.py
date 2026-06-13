from sluice_core.models import (
    AppSpec,
    AppStatus,
    K8sPlacementSpec,
    KubernetesCandidate,
    Toleration,
    VmCandidate,
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
    # default placement: one in-cluster spot candidate that schedules anywhere
    assert len(app.placement) == 1
    cand = app.placement[0]
    assert cand.type == "kubernetes" and cand.provider == "in-cluster"
    assert cand.spec.pricing == "spot" and cand.spec.node_selectors == [{}]


def test_placement_is_ordered_discriminated_union():
    app = AppSpec(
        name="m",
        image="i",
        placement=[
            KubernetesCandidate(
                provider="in-cluster",
                spec=K8sPlacementSpec(
                    pricing="spot",
                    node_selectors=[{"gpu": "l4", "lifecycle": "spot"}, {"cloud.google.com/gke-spot": "true"}],
                    tolerations=[Toleration(key="nvidia.com/gpu")],
                    schedule_grace_s=120,
                ),
            ),
            VmCandidate(provider="gce", spec=VmPlacementSpec(pricing="spot", machine_type="g2", regions=["r1"])),
            KubernetesCandidate(provider="gke-east", spec=K8sPlacementSpec(pricing="on-demand")),
        ],
    )
    # order is preserved exactly (list index = priority)
    assert [c.type for c in app.placement] == ["kubernetes", "vm", "kubernetes"]
    assert [c.provider for c in app.placement] == ["in-cluster", "gce", "gke-east"]
    # ordered node selectors kept in author order
    assert app.placement[0].spec.node_selectors[0] == {"gpu": "l4", "lifecycle": "spot"}
    assert app.placement[0].spec.tolerations[0].effect == "NoSchedule"
    assert app.placement[0].spec.schedule_grace_s == 120
    assert app.placement[1].spec.machine_type == "g2" and app.placement[1].spec.workers_per_vm == 1
    assert app.placement[2].spec.pricing == "on-demand"


def test_appstatus_defaults():
    s = AppStatus()
    assert s.phase == "Ready" and s.reason is None and s.candidate is None
    assert s.workers == {} and s.queue.visible == 0
