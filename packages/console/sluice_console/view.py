from __future__ import annotations

from pydantic import BaseModel
from sluice_core.models import AppSpec, QueueDepth, WorkerState, WorkerStatus

_STUCK = {WorkerState.pending, WorkerState.unschedulable}


def scale_status(app: AppSpec, workers: list[WorkerStatus], *, visible: int) -> str:
    if app.desired_state == "Paused":
        return "paused"
    if any(w.state in _STUCK and w.age_s > app.scaling.startup_grace_s for w in workers):
        return "held"
    if any(w.state in _STUCK for w in workers):
        return "holding"
    if visible > 0 and not any(w.state == WorkerState.running for w in workers):
        return "scaling"
    return "ready"


class AppView(BaseModel):
    name: str
    desired_state: str
    # Authoritative controller verdict from the persisted AppStatus (the source of truth for *why*):
    #   phase — Ready | Scaling | Held | Paused | Draining (None when the controller has never written)
    #   reason / candidate — the operator-facing "why isn't this scaling?" and the active placement key
    #   updated_at — wall clock the controller last wrote (0.0 ⇒ never written / unknown ⇒ stale)
    phase: str | None = None
    reason: str | None = None
    candidate: str | None = None
    updated_at: float = 0.0
    # Live, k8s-derived hint recomputed from current worker states + queue depth. Secondary to `phase`:
    # it stays fresh even when the controller is down (persisted status stale), but it cannot see the
    # controller's verdict (stockout/registration errors/active candidate).
    scale_status: str
    queue: QueueDepth
    workers: dict[str, int]


class AppDetail(AppView):
    worker_list: list[WorkerStatus]
