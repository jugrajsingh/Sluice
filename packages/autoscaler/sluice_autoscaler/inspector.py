from __future__ import annotations

from sluice_core.models import WorkerState

_UNHEALTHY_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "RunContainerError"}


def map_pod_state(pod: dict) -> tuple[WorkerState, str | None]:
    st = pod.get("status", {})
    phase = st.get("phase")
    conds = {c.get("type"): c for c in st.get("conditions", [])}
    waitings = [cs.get("state", {}).get("waiting", {}).get("reason") for cs in st.get("containerStatuses", [])]
    waitings = [w for w in waitings if w]

    if phase == "Succeeded":
        return WorkerState.exited, None
    if phase == "Failed":
        return WorkerState.failed, st.get("reason")

    sched = conds.get("PodScheduled")
    if sched and sched.get("status") == "False" and sched.get("reason") == "Unschedulable":
        return WorkerState.unschedulable, sched.get("message")

    if any(w in _UNHEALTHY_WAITING for w in waitings):
        return WorkerState.unhealthy, waitings[0]
    if "ContainerCreating" in waitings or "PodInitializing" in waitings:
        return WorkerState.starting, None

    if phase == "Running":
        ready = conds.get("Ready")
        if ready and ready.get("status") == "True":
            return WorkerState.running, None
        return WorkerState.unhealthy, "NotReady"
    return WorkerState.pending, None
