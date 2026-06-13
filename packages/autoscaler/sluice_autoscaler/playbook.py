"""The pure placement playbook: spec + observations -> actions. No I/O here."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field
from sluice_core.models import AppSpec, QueueDepth, VmRecord, VmState, WorkerState, WorkerStatus

from .placement import Candidate, candidate_key, expand_candidates

_REAP = {WorkerState.exited, WorkerState.failed}
_STUCK = {WorkerState.pending, WorkerState.unschedulable}
_POD_ACTIVE = {WorkerState.running, WorkerState.starting}
_VM_LIVE = {VmState.provisioning, VmState.booting, VmState.running}


class VmView(BaseModel):
    record: VmRecord
    phase: str | None = None  # heartbeat phase: installing|running|workers_exited|stopping
    workers_running: int = 0


class Observed(BaseModel):
    pods: list[WorkerStatus] = Field(default_factory=list)
    vms: list[VmView] = Field(default_factory=list)
    depth: QueueDepth = Field(default_factory=QueueDepth)


class ReapPod(BaseModel):
    pod: str


class RemoveStuckPod(BaseModel):
    pod: str


class CreatePods(BaseModel):
    candidate: Candidate
    count: int


class MarkStockout(BaseModel):
    candidate_key: str
    reason: str


class ProvisionVms(BaseModel):
    candidate: Candidate
    count: int


class CommandVm(BaseModel):
    vm_id: str
    command: Literal["start_workers", "shutdown"]


class DestroyVm(BaseModel):
    vm_id: str


Action = ReapPod | RemoveStuckPod | CreatePods | MarkStockout | ProvisionVms | CommandVm | DestroyVm


class PlacementPlan(BaseModel):
    actions: list[Action] = Field(default_factory=list)
    phase: str = "Ready"
    reason: str | None = None
    candidate: str | None = None


def _vm_spec(app: AppSpec):
    return next((c.spec for c in app.placement if c.type == "vm"), None)


def _vm_key(app: AppSpec, rec: VmRecord) -> str:
    # Mirrors candidate_key() for a vm Candidate (selector segment is always "none").
    return f"vm/{rec.provider}/{rec.region}/none/{app.resources.gpu_type or 'none'}/{rec.pricing}"


def plan(
    app: AppSpec,
    observed: Observed,
    *,
    stocked: dict[str, str],
    now: float,
    cooldown_until: float,
    boot_deadline_s: int = 600,
) -> PlacementPlan:
    actions: list[Action] = []
    vm_spec = _vm_spec(app)
    wpv = vm_spec.workers_per_vm if vm_spec else 1
    max_vms = vm_spec.max_vms if vm_spec else 0

    # 1. Reap exited/failed pods — always, first.
    actions += [ReapPod(pod=p.pod) for p in observed.pods if p.state in _REAP]

    # 2. VM hygiene: destroy stopped; destroy+mark preempted and boot-deadline breaches.
    for v in observed.vms:
        rec = v.record
        if rec.state == VmState.stopped:
            actions.append(DestroyVm(vm_id=rec.id))
        elif rec.state == VmState.preempted:
            actions.append(DestroyVm(vm_id=rec.id))
            actions.append(MarkStockout(candidate_key=_vm_key(app, rec), reason="preempted"))
        elif (
            rec.state in (VmState.provisioning, VmState.booting)
            and v.phase is None
            and now - rec.created_at > boot_deadline_s
        ):
            actions.append(DestroyVm(vm_id=rec.id))
            actions.append(MarkStockout(candidate_key=_vm_key(app, rec), reason="boot-deadline"))

    # 3. Stuck pods: mark their candidate, remove, retry elsewhere this same cycle.
    newly_marked: dict[str, str] = {}
    for p in observed.pods:
        if p.state in _STUCK and p.age_s > app.scaling.schedule_grace_s:
            actions.append(RemoveStuckPod(pod=p.pod))
            if p.candidate:
                reason = p.reason or "schedule-grace-exceeded"
                actions.append(MarkStockout(candidate_key=p.candidate, reason=reason))
                newly_marked[p.candidate] = reason

    if app.desired_state == "Paused":
        return PlacementPlan(actions=actions, phase="Paused")

    # 4. Capacity accounting (pods + VM workers together).
    grace = app.scaling.schedule_grace_s
    live_pods = sum(1 for p in observed.pods if p.state in _POD_ACTIVE or (p.state in _STUCK and p.age_s <= grace))
    vm_workers = 0
    idle_vms: list[VmView] = []
    live_vm_count = 0
    for v in observed.vms:
        if v.record.state not in _VM_LIVE:
            continue
        live_vm_count += 1
        if v.phase == "workers_exited":
            idle_vms.append(v)
        elif v.phase == "running":
            vm_workers += v.workers_running or wpv
        else:  # provisioning/booting: count promised capacity
            vm_workers += wpv

    mpw = max(app.scaling.messages_per_worker, 1)
    desired = math.ceil(observed.depth.visible / mpw)
    if app.scaling.max_workers > 0:
        desired = min(desired, app.scaling.max_workers)
    need = desired - (live_pods + vm_workers)

    # 5. Warm restarts beat provisioning.
    for v in idle_vms:
        if need <= 0:
            break
        actions.append(CommandVm(vm_id=v.record.id, command="start_workers"))
        need -= wpv

    if need <= 0:
        return PlacementPlan(actions=actions, phase="Ready")

    if now < cooldown_until:
        return PlacementPlan(actions=actions, phase="Scaling", reason="cooldown")

    # 6. Walk candidates from the top, skipping stockout marks (incl. this cycle's).
    blocked = {**stocked, **newly_marked}
    for cand in expand_candidates(app):
        key = candidate_key(cand)
        if key in blocked:
            continue
        if cand.type == "kubernetes":
            actions.append(CreatePods(candidate=cand, count=min(app.scaling.scale_up_count, need)))
            return PlacementPlan(actions=actions, phase="Scaling", candidate=key)
        n = min(math.ceil(need / wpv), max(max_vms - live_vm_count, 0))
        if n <= 0:
            continue  # at VM cap for this app; try next candidate
        actions.append(ProvisionVms(candidate=cand, count=n))
        return PlacementPlan(actions=actions, phase="Scaling", candidate=key)

    reasons = ", ".join(sorted(set(blocked.values()))) or "no candidates"
    return PlacementPlan(actions=actions, phase="Held", reason=reasons)
