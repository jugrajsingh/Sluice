"""The pure placement playbook: spec + observations -> actions. No I/O here."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field
from sluice_core.models import AppSpec, QueueDepth, VmRecord, VmState, WorkerState, WorkerStatus

from .inspector import classify_unschedulable
from .placement import Candidate, candidate_key, expand_candidates

_REAP = {WorkerState.exited, WorkerState.failed}
_STUCK = {WorkerState.pending, WorkerState.unschedulable}
_POD_ACTIVE = {WorkerState.running, WorkerState.starting}
_VM_LIVE = {VmState.provisioning, VmState.booting, VmState.running}


class VmView(BaseModel):
    record: VmRecord
    phase: str | None = None  # heartbeat phase: installing|running|workers_exited|stopping
    workers_running: int = 0
    # Hung classifications set by the controller (it holds the clock + heartbeat timestamps, ADR-012):
    unreachable: bool = False  # RUNNING per the prober but heartbeats are stale (silent hang)
    wedged: bool = False  # heartbeating but workers keep exiting past the restart cap (likely OOM/misconfig)


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
    desired: int = 0  # the final computed desired-instance count (0 on the Paused early-return)


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
    desired_floor: int = 0,
    desired_override: int | None = None,
) -> PlacementPlan:
    actions: list[Action] = []

    def grace_for(p: WorkerStatus) -> int:  # noqa: ARG001 - one app-level grace; signature kept for call sites
        return app.scaling.startup_grace_s

    # 1. Reap exited/failed pods — always, first.
    actions += [ReapPod(pod=p.pod) for p in observed.pods if p.state in _REAP]

    # 2. VM hygiene: destroy stopped; destroy+mark preempted and boot-deadline breaches.
    destroying: set[str] = set()  # VMs being torn down this cycle — exclude from live capacity
    vm_marked: dict[str, str] = {}  # vm candidate keys stocked out this cycle — block re-provision
    for v in observed.vms:
        rec = v.record
        if rec.state == VmState.stopped:
            actions.append(DestroyVm(vm_id=rec.id))
            destroying.add(rec.id)
        elif rec.state == VmState.preempted:
            actions.append(DestroyVm(vm_id=rec.id))
            actions.append(MarkStockout(candidate_key=_vm_key(app, rec), reason="preempted"))
            destroying.add(rec.id)
            vm_marked[_vm_key(app, rec)] = "preempted"
        elif (
            rec.state in (VmState.provisioning, VmState.booting)
            and v.phase is None
            and now - rec.created_at > app.scaling.startup_grace_s
        ):
            actions.append(DestroyVm(vm_id=rec.id))
            actions.append(MarkStockout(candidate_key=_vm_key(app, rec), reason="boot-deadline"))
            destroying.add(rec.id)
            vm_marked[_vm_key(app, rec)] = "boot-deadline"

    # 3. Stuck pods: classify the scheduler's verdict and act accordingly.
    #    - capacity exhaustion -> stock out (after the candidate's grace; immediately if the node
    #      group is already maxed) and remove, so we retry the next candidate this same cycle.
    #    - untolerated taint -> a config bug, NOT a stockout: surface it and skip the candidate
    #      this cycle only; never persist a mark and never churn the pod (the operator must fix it).
    newly_marked: dict[str, str] = {}
    config_errors: dict[str, str] = {}
    removing: set[str] = set()
    for p in observed.pods:
        if p.state not in _STUCK:
            continue
        kind = classify_unschedulable(p.reason)
        if kind == "taint":
            if p.candidate:
                config_errors[p.candidate] = p.reason or "untolerated taint"
            continue
        if kind == "terminal_capacity" or p.age_s > grace_for(p):
            actions.append(RemoveStuckPod(pod=p.pod))
            removing.add(p.pod)
            if p.candidate:
                reason = p.reason or "schedule-grace-exceeded"
                actions.append(MarkStockout(candidate_key=p.candidate, reason=reason))
                newly_marked[p.candidate] = reason

    if app.desired_state == "Paused":
        return PlacementPlan(actions=actions, phase="Paused")  # desired left 0 — we don't scale a paused app

    # 4. Capacity accounting in **units** (1 instance = 1 pod = 1 VM = 1 unit; a unit packs
    #    `worker.instances` replicas internally, so messagesPerInstance is tuned per packed unit).
    #    Pods being removed this cycle don't count; a stuck pod counts only inside the startup grace.
    live_pods = sum(
        1
        for p in observed.pods
        if p.pod not in removing and (p.state in _POD_ACTIVE or (p.state in _STUCK and p.age_s <= grace_for(p)))
    )
    serving_vm_units = 0
    idle_vms: list[VmView] = []
    for v in observed.vms:
        if v.record.state not in _VM_LIVE or v.record.id in destroying:
            continue  # not live, or being torn down this cycle
        if v.unreachable or v.wedged:
            continue  # hung — counts as neither serving nor restartable, so `need` drives a replacement
        if v.phase == "workers_exited":
            idle_vms.append(v)  # warm but idle — restartable, doesn't count as serving
        else:  # running or provisioning/booting -> one serving unit
            serving_vm_units += 1

    if desired_override is not None:
        # SLA-derived desired (batch-aware path): already encodes the hard cap; skip plain formula.
        desired = desired_override
    else:
        mpw = max(app.scaling.messages_per_instance, 1)
        desired = math.ceil(observed.depth.visible / mpw)
    # Warm floor + scale-down-stabilization peak (held by the controller); then the hard ceiling.
    desired = max(desired, app.scaling.min_instances, desired_floor)
    if app.scaling.max_instances > 0:
        desired = min(desired, app.scaling.max_instances)
    live_units = live_pods + serving_vm_units
    need = desired - live_units

    # 5. Warm restarts beat provisioning (one unit per VM).
    for v in idle_vms:
        if need <= 0:
            break
        actions.append(CommandVm(vm_id=v.record.id, command="start_workers"))
        need -= 1

    if need <= 0:
        return PlacementPlan(actions=actions, phase="Ready", desired=desired)

    if now < cooldown_until:
        return PlacementPlan(actions=actions, phase="Scaling", reason="cooldown", desired=desired)

    # 6. Walk candidates from the top, skipping stockouts (persisted + this cycle's) and any
    #    candidate with a surfaced config error (this cycle only — config bugs aren't stockouts).
    blocked = {**stocked, **newly_marked, **config_errors, **vm_marked}
    config_summary = "; ".join(sorted(set(config_errors.values()))) or None
    for cand in expand_candidates(app):
        key = candidate_key(cand)
        if key in blocked:
            continue
        if cand.type == "kubernetes":
            actions.append(CreatePods(candidate=cand, count=min(app.scaling.max_scale_up_per_cycle, need)))
            return PlacementPlan(
                actions=actions, phase="Scaling", candidate=key, reason=config_summary, desired=desired
            )
        # Per-cycle cap applies to VMs too (fixes the old VM burst-storm: it had NO per-cycle cap). `need`
        # already respects the total cap because `desired` was clamped to maxInstances above, but keep an
        # explicit maxInstances-headroom guard for safety (one VM = one unit).
        headroom = (app.scaling.max_instances - live_units) if app.scaling.max_instances > 0 else need
        n = min(need, app.scaling.max_scale_up_per_cycle, headroom)
        if n <= 0:
            continue  # at the instance ceiling for this app; try next candidate
        actions.append(ProvisionVms(candidate=cand, count=n))
        return PlacementPlan(actions=actions, phase="Scaling", candidate=key, reason=config_summary, desired=desired)

    reasons = ", ".join(sorted(set(blocked.values()))) or "no candidates"
    return PlacementPlan(actions=actions, phase="Held", reason=reasons, desired=desired)
