from __future__ import annotations

from pydantic import BaseModel
from sluice_core.models import AppSpec, QueueDepth, WorkerState, WorkerStatus

_STUCK = {WorkerState.pending, WorkerState.unschedulable}


def scale_status(app: AppSpec, workers: list[WorkerStatus], *, visible: int) -> str:
    if app.desired_state == "Paused":
        return "paused"
    if any(w.state in _STUCK and w.age_s > app.scaling.schedule_grace_s for w in workers):
        return "held"
    if any(w.state in _STUCK for w in workers):
        return "holding"
    if visible > 0 and not any(w.state == WorkerState.running for w in workers):
        return "scaling"
    return "ready"


class AppView(BaseModel):
    name: str
    desired_state: str
    scale_status: str
    queue: QueueDepth
    workers: dict[str, int]


class AppDetail(AppView):
    worker_list: list[WorkerStatus]
