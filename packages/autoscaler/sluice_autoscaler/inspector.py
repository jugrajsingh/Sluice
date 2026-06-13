from __future__ import annotations

from typing import Literal

from sluice_core.models import WorkerState

_UNHEALTHY_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "RunContainerError"}

# Substrings of a Pod's `PodScheduled=Unschedulable` message, by what they mean for placement.
# Order matters: taint (config bug) is checked before capacity so a tainted-node message that
# also mentions capacity is treated as the config bug it is.
_TAINT = ("untolerated taint",)
_TERMINAL_CAPACITY = ("max node group size reached", "NotTriggerScaleUp")
_CAPACITY = (
    "Insufficient",  # e.g. "Insufficient nvidia.com/gpu"
    "didn't match Pod's node affinity/selector",
    "didn't match pod affinity",
    "ZONE_RESOURCE_POOL_EXHAUSTED",
    "PreemptionNotHelpful",
)

UnschedulableKind = Literal["taint", "terminal_capacity", "capacity", "other"]


def classify_unschedulable(message: str | None) -> UnschedulableKind:
    """Classify an Unschedulable message so the playbook can decide what to do.

    - `taint`: the pod can't tolerate a node taint — a configuration bug, NOT a stockout.
    - `terminal_capacity`: the node group is already at max size — stock out now, don't wait.
    - `capacity`: not enough capacity (yet) — stock out only after the per-candidate grace.
    - `other`: unrecognized / plain Pending — treated as capacity-after-grace by the caller.
    """
    msg = message or ""
    if any(s in msg for s in _TAINT):
        return "taint"
    if any(s in msg for s in _TERMINAL_CAPACITY):
        return "terminal_capacity"
    if any(s in msg for s in _CAPACITY):
        return "capacity"
    return "other"


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
