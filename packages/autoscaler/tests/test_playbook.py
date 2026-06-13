from sluice_autoscaler.placement import candidate_key, expand_candidates
from sluice_autoscaler.playbook import (
    CommandVm,
    CreatePods,
    DestroyVm,
    MarkStockout,
    Observed,
    ProvisionVms,
    ReapPod,
    RemoveStuckPod,
    VmView,
    plan,
)
from sluice_core.models import (
    AppSpec,
    K8sPlacementSpec,
    KubernetesCandidate,
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


def _app(with_vm=True):
    placement = [
        KubernetesCandidate(
            provider="in-cluster",
            spec=K8sPlacementSpec(pricing="spot", node_selectors=[{"zone": "z1"}, {"zone": "z2"}]),
        )
    ]
    if with_vm:
        placement.append(
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(
                    pricing="spot",
                    machine_type="g2",
                    regions=["r1", "r2"],
                    workers_per_vm=2,
                    max_vms=3,
                    linger_seconds=300,
                ),
            )
        )
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_worker=10, scale_up_count=3, schedule_grace_s=180),
        placement=placement,
    )


def _keys(app):
    return [candidate_key(c) for c in expand_candidates(app)]


def _pod(state, age=0, cand=None, name="p", reason=None):
    return WorkerStatus(pod=name, state=state, age_s=age, candidate=cand, reason=reason)


def _k8s_app(grace=180, selectors=None):
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_worker=10, scale_up_count=3, schedule_grace_s=180),
        placement=[
            KubernetesCandidate(
                provider="in-cluster",
                spec=K8sPlacementSpec(
                    pricing="spot",
                    node_selectors=selectors or [{"zone": "z1"}, {"zone": "z2"}],
                    schedule_grace_s=grace,
                ),
            )
        ],
    )


def _vm(vm_id="v1", state=VmState.running, phase=None, workers=0, created=0.0, region="r1"):
    rec = VmRecord(
        id=vm_id,
        app="m",
        provider="gce",
        region=region,
        pricing="spot",
        machine_type="g2",
        state=state,
        created_at=created,
    )
    return VmView(record=rec, phase=phase, workers_running=workers)


def _of(t, actions):
    return [a for a in actions if isinstance(a, t)]


def test_reap_exited_pods_always():
    p = plan(_app(), Observed(pods=[_pod(WorkerState.exited)], depth=QueueDepth()), stocked={}, now=0, cooldown_until=0)
    assert _of(ReapPod, p.actions)


def test_backlog_creates_pods_on_first_candidate():
    p = plan(_app(), Observed(depth=QueueDepth(visible=100)), stocked={}, now=0, cooldown_until=0)
    creates = _of(CreatePods, p.actions)
    assert creates and creates[0].count == 3  # capped by scale_up_count
    assert creates[0].candidate.selector == {"zone": "z1"} and p.phase == "Scaling"


def test_stuck_pod_marks_and_removes_and_advances():
    app = _app()
    k_z1 = _keys(app)[0]
    p = plan(
        app,
        Observed(pods=[_pod(WorkerState.unschedulable, age=999, cand=k_z1)], depth=QueueDepth(visible=100)),
        stocked={},
        now=0,
        cooldown_until=0,
    )
    assert _of(RemoveStuckPod, p.actions)
    assert any(m.candidate_key == k_z1 for m in _of(MarkStockout, p.actions))
    creates = _of(CreatePods, p.actions)
    assert creates and creates[0].candidate.selector == {"zone": "z2"}  # next selector


def test_k8s_exhausted_escalates_to_vm():
    app = _app()
    ks = _keys(app)
    stocked = {ks[0]: "x", ks[1]: "x"}  # both k8s selectors marked
    p = plan(app, Observed(depth=QueueDepth(visible=100)), stocked=stocked, now=0, cooldown_until=0)
    prov = _of(ProvisionVms, p.actions)
    assert prov and prov[0].candidate.location == "r1"
    assert prov[0].count == 3  # ceil(10/2)=5 -> capped maxVms=3


def test_all_candidates_stocked_is_held():
    app = _app()
    p = plan(
        app, Observed(depth=QueueDepth(visible=10)), stocked={k: "out" for k in _keys(app)}, now=0, cooldown_until=0
    )
    assert p.phase == "Held" and not _of(CreatePods, p.actions) and not _of(ProvisionVms, p.actions)


def test_preempted_vm_destroyed_and_marked():
    p = plan(
        _app(), Observed(vms=[_vm(state=VmState.preempted)], depth=QueueDepth()), stocked={}, now=0, cooldown_until=0
    )
    assert _of(DestroyVm, p.actions) and _of(MarkStockout, p.actions)


def test_boot_deadline_exceeded_destroys_and_marks():
    p = plan(
        _app(),
        Observed(vms=[_vm(state=VmState.provisioning, phase=None, created=0)], depth=QueueDepth()),
        stocked={},
        now=10_000,
        cooldown_until=0,
        boot_deadline_s=600,
    )
    assert _of(DestroyVm, p.actions) and _of(MarkStockout, p.actions)


def test_idle_vm_warm_restarts_when_queue_refills():
    p = plan(
        _app(),
        Observed(vms=[_vm(phase="workers_exited", workers=0)], depth=QueueDepth(visible=5)),
        stocked={},
        now=0,
        cooldown_until=0,
    )
    cmds = _of(CommandVm, p.actions)
    assert cmds and cmds[0].command == "start_workers"
    assert not _of(ProvisionVms, p.actions)  # warm VM beats new provision


def test_stopped_vm_is_destroyed():
    p = plan(
        _app(), Observed(vms=[_vm(state=VmState.stopped)], depth=QueueDepth()), stocked={}, now=0, cooldown_until=0
    )
    assert _of(DestroyVm, p.actions)


def test_paused_never_creates():
    app = _app()
    app.desired_state = "Paused"
    p = plan(
        app,
        Observed(pods=[_pod(WorkerState.exited)], depth=QueueDepth(visible=100)),
        stocked={},
        now=0,
        cooldown_until=0,
    )
    assert p.phase == "Paused"
    assert not _of(CreatePods, p.actions) and not _of(ProvisionVms, p.actions)
    assert _of(ReapPod, p.actions)  # reaping continues


def test_cooldown_blocks_creates():
    p = plan(_app(), Observed(depth=QueueDepth(visible=100)), stocked={}, now=5, cooldown_until=10)
    assert not _of(CreatePods, p.actions) and p.phase == "Scaling" and p.reason == "cooldown"


def test_enough_capacity_is_ready():
    vms = [_vm(phase="running", workers=2)]
    pods = [_pod(WorkerState.running, name=f"p{i}") for i in range(3)]
    p = plan(_app(), Observed(pods=pods, vms=vms, depth=QueueDepth(visible=40)), stocked={}, now=0, cooldown_until=0)
    assert p.phase == "Ready" and not _of(CreatePods, p.actions)  # 5 workers >= ceil(40/10)=4


def test_max_workers_caps_desired_across_substrates():
    app = _app()
    app.scaling.max_workers = 2
    p = plan(app, Observed(depth=QueueDepth(visible=1000)), stocked={}, now=0, cooldown_until=0)
    creates = _of(CreatePods, p.actions)
    assert creates and creates[0].count == 2  # capped by maxWorkers, not scale_up_count


# --- unschedulable classification (Task 4) ---


def test_terminal_capacity_stockouts_before_grace_elapses():
    app = _k8s_app()  # grace 180
    k0 = _keys(app)[0]
    stuck = _pod(
        WorkerState.unschedulable, age=5, cand=k0, reason="pod didn't trigger scale-up: max node group size reached"
    )
    p = plan(app, Observed(pods=[stuck], depth=QueueDepth(visible=100)), stocked={}, now=0, cooldown_until=0)
    # age 5 << grace 180, but the node group is maxed — no point waiting it out
    assert any(a.pod == "p" for a in _of(RemoveStuckPod, p.actions))
    assert any(m.candidate_key == k0 for m in _of(MarkStockout, p.actions))


def test_capacity_within_grace_waits_without_stockout():
    app = _k8s_app()  # grace 180
    k0 = _keys(app)[0]
    stuck = _pod(
        WorkerState.unschedulable, age=5, cand=k0, reason="0/5 nodes are available: 5 Insufficient nvidia.com/gpu."
    )
    p = plan(app, Observed(pods=[stuck], depth=QueueDepth(visible=100)), stocked={}, now=0, cooldown_until=0)
    # still inside the grace window — wait for the autoscaler, don't stock out or remove
    assert not _of(RemoveStuckPod, p.actions) and not _of(MarkStockout, p.actions)


def test_per_candidate_grace_overrides_app_default():
    app = _k8s_app(grace=30)  # candidate grace 30, app-level default 180
    k0 = _keys(app)[0]
    stuck = _pod(WorkerState.unschedulable, age=60, cand=k0, reason="Insufficient nvidia.com/gpu")
    p = plan(app, Observed(pods=[stuck], depth=QueueDepth(visible=100)), stocked={}, now=0, cooldown_until=0)
    # age 60 > candidate grace 30 (though < app default 180) -> stock out
    assert any(m.candidate_key == k0 for m in _of(MarkStockout, p.actions))


def test_untolerated_taint_surfaces_and_advances_without_stockout():
    app = _k8s_app()
    k0 = _keys(app)[0]
    taint = "0/3 nodes are available: 3 node(s) had untolerated taint {nvidia.com/gpu: present}"
    stuck = _pod(WorkerState.unschedulable, age=999, cand=k0, reason=taint)
    p = plan(app, Observed(pods=[stuck], depth=QueueDepth(visible=100)), stocked={}, now=0, cooldown_until=0)
    # config bug: never stock out, never churn the pod — but skip the bad selector this cycle
    assert not _of(MarkStockout, p.actions)
    assert not any(a.pod == "p" for a in _of(RemoveStuckPod, p.actions))
    creates = _of(CreatePods, p.actions)
    assert creates and creates[0].candidate.selector == {"zone": "z2"}  # advanced past the misconfigured selector
    assert p.reason and "untolerated taint" in p.reason  # surfaced to the operator


def test_all_selectors_misconfigured_holds_with_config_reason():
    app = _k8s_app(selectors=[{"zone": "z1"}])
    k0 = _keys(app)[0]
    taint = "node(s) had untolerated taint {nvidia.com/gpu: present}"
    stuck = _pod(WorkerState.unschedulable, age=999, cand=k0, reason=taint)
    p = plan(app, Observed(pods=[stuck], depth=QueueDepth(visible=100)), stocked={}, now=0, cooldown_until=0)
    assert not _of(MarkStockout, p.actions) and not _of(CreatePods, p.actions)
    assert p.phase == "Held" and "untolerated taint" in p.reason
