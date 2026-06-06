from sluice_autoscaler.inspector import map_pod_state
from sluice_core.models import WorkerState


def _pod(phase, *, conditions=None, waiting=None, scheduled_reason=None):
    return {
        "status": {
            "phase": phase,
            "conditions": conditions or [],
            "containerStatuses": [{"state": {"waiting": {"reason": waiting}} if waiting else {"running": {}}}],
        },
        "_unschedulable_reason": scheduled_reason,
    }


def test_running_ready():
    p = _pod("Running", conditions=[{"type": "Ready", "status": "True"}])
    assert map_pod_state(p)[0] == WorkerState.running


def test_running_not_ready_is_unhealthy():
    p = _pod("Running", conditions=[{"type": "Ready", "status": "False"}])
    assert map_pod_state(p)[0] == WorkerState.unhealthy


def test_unschedulable_with_reason():
    p = _pod(
        "Pending",
        conditions=[
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "ZONE_RESOURCE_POOL_EXHAUSTED",
            }
        ],
    )
    state, reason = map_pod_state(p)
    assert state == WorkerState.unschedulable and "ZONE_RESOURCE_POOL_EXHAUSTED" in reason


def test_pending_plain():
    assert map_pod_state(_pod("Pending"))[0] == WorkerState.pending


def test_starting_on_containercreating():
    assert map_pod_state(_pod("Pending", waiting="ContainerCreating"))[0] == WorkerState.starting


def test_crashloop_is_unhealthy():
    assert map_pod_state(_pod("Running", waiting="CrashLoopBackOff"))[0] == WorkerState.unhealthy


def test_succeeded_is_exited():
    assert map_pod_state(_pod("Succeeded"))[0] == WorkerState.exited


def test_failed():
    assert map_pod_state(_pod("Failed"))[0] == WorkerState.failed
