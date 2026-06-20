from sluice_console.view import scale_status
from sluice_core.models import AppSpec, ScalingSpec, WorkerState, WorkerStatus


def _app(paused=False, grace=180):
    a = AppSpec(name="m", image="i", handler="h:H", scaling=ScalingSpec(startup_grace_s=grace))
    a.desired_state = "Paused" if paused else "Ready"
    return a


def _w(state, age_s=0):
    return WorkerStatus(pod="p", state=state, age_s=age_s)


def test_paused():
    assert scale_status(_app(paused=True), [], visible=0) == "paused"


def test_held_on_stuck_worker():
    assert scale_status(_app(), [_w(WorkerState.unschedulable, age_s=999)], visible=10) == "held"


def test_holding_when_pending_within_grace():
    assert scale_status(_app(), [_w(WorkerState.pending, age_s=5)], visible=10) == "holding"


def test_scaling_when_backlog_and_no_workers():
    assert scale_status(_app(), [], visible=10) == "scaling"


def test_ready_when_idle():
    assert scale_status(_app(), [_w(WorkerState.running)], visible=0) == "ready"
