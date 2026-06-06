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
    NodePoolSpec,
    PlacementSpec,
    QueueDepth,
    ResourcesSpec,
    ScalingSpec,
    VmPlacementSpec,
    VmRecord,
    VmState,
    WorkerState,
    WorkerStatus,
)


def _app(mode="both"):
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="l4"),
        scaling=ScalingSpec(messages_per_worker=10, scale_up_count=3, schedule_grace_s=180),
        placement=PlacementSpec(
            mode=mode,
            pricing=["spot"],
            kubernetes=[NodePoolSpec(pricing="spot", selector={"s": "1"}, zones=["z1", "z2"])],
            vm=VmPlacementSpec(
                provider="gce", machine_type="g2", regions=["r1", "r2"], workers_per_vm=2, max_vms=3, linger_seconds=300
            ),
        ),
    )


def _keys(app):
    return [candidate_key(c) for c in expand_candidates(app)]


def _pod(state, age=0, cand=None, name="p"):
    return WorkerStatus(pod=name, state=state, age_s=age, candidate=cand)


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
    assert creates[0].candidate.location == "z1" and p.phase == "Scaling"


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
    assert creates and creates[0].candidate.location == "z2"  # next zone


def test_k8s_exhausted_escalates_to_vm():
    app = _app()
    ks = _keys(app)
    stocked = {ks[0]: "x", ks[1]: "x"}  # both zones marked
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
