from __future__ import annotations

import json
import logging
import time

from sluice_core.errors import KeyNotFound, ProvisionFailure
from sluice_core.interfaces import (
    AppRegistry,
    Cache,
    ClusterInspector,
    ComputeProvider,
    ObjectStore,
    Queue,
)
from sluice_core.models import AppSpec, AppStatus, Toleration, VmRecord, VmState, WorkerState
from sluice_core.vm_paths import heartbeat_key
from sluice_core.vm_tracker import VmTracker

from .batch_scale import sla_desired
from .metrics import HOLDS, RECONCILE_SECONDS, SCALE_UP_PODS, STOCKOUTS, VMS, WORKERS
from .placement import candidate_key, expand_candidates
from .playbook import (
    CommandVm,
    CreatePods,
    DestroyVm,
    MarkStockout,
    Observed,
    PlacementPlan,
    ProvisionVms,
    ReapPod,
    RemoveStuckPod,
    VmView,
    plan,
)
from .stockout import StockoutBoard
from .vm_commands import VmCommander

IN_CLUSTER = "in-cluster"

logger = logging.getLogger(__name__)


class PodManager:
    """Bare-pod lifecycle on the k8s substrate."""

    async def create_pods(
        self,
        app: AppSpec,
        n: int,
        *,
        selector: dict[str, str],
        candidate_key: str = "",
        tolerations: list[Toleration] | None = None,
        **kw,
    ) -> None: ...
    async def delete_pods(self, app: AppSpec, names: list[str]) -> None: ...


def _summarize(workers: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for w in workers:
        out[w.state.value] = out.get(w.state.value, 0) + 1
    return out


class Controller:
    def __init__(
        self,
        *,
        registry: AppRegistry,
        queue: Queue,
        inspector: ClusterInspector,
        pods: PodManager,
        clusters: dict[str, tuple[PodManager, ClusterInspector]] | None = None,
        compute: ComputeProvider | None = None,
        commander: VmCommander | None = None,
        cache: Cache | None = None,
        store: ObjectStore | None = None,
        tracker: VmTracker | None = None,
        stockout_ttl_s: int = 600,
        vm_root: str = "sluice",
    ) -> None:
        self._reg = registry
        self._q = queue
        self._inspect = inspector
        self._pods = pods
        # Per-cluster (pod manager, inspector). `pods`/`inspector` are the in-cluster handle;
        # `clusters` adds external clusters (kubeconfig-backed), keyed by name. A k8s placement
        # candidate's `cluster` selects the handle; vm candidates use the ComputeProvider instead.
        self._clusters: dict[str, tuple[PodManager, ClusterInspector]] = {
            IN_CLUSTER: (pods, inspector),
            **(clusters or {}),
        }
        self._compute = compute
        self._commander = commander
        self._store = store
        # Durable per-(app,region) VM-tracking ledger (ADR-012): the prober stays the source of truth;
        # the ledger records what we provisioned + reaped + prober errors (debugging / future patches).
        self._tracker = tracker
        self._board = StockoutBoard(cache=cache, ttl_s=stockout_ttl_s) if cache else None
        self._root = vm_root
        self._cooldown_until: dict[str, float] = {}
        # Scale-down stabilization (anti-flap), keyed by app: (peak_desired, hold_until). A transient
        # queue dip keeps the held peak as a desired_floor until the window elapses; a rise refreshes it.
        self._desired_peak: dict[str, tuple[float, float]] = {}
        # Hung-VM tracking (ADR-012), keyed by vm_id, pruned each cycle to the observed set:
        self._exited_cycles: dict[str, int] = {}  # consecutive workers_exited observations (→ wedged)
        self._reset_at: dict[str, float] = {}  # when we last issued a reboot (reset once before deleting)
        self._unreachable_seen: set[str] = set()  # log the unreachable transition once, not every cycle
        # Last-logged per-app status tuple (Healthy, Starting, Draining, Unreachable). The reconcile loop
        # runs every ~15s; we emit the status line ONLY when this counts-tuple changes, so a steady fleet
        # is silent and a real shift is visible.
        self._last_status: dict[str, tuple[int, int, int, int]] = {}

    async def _observe_vms(self, app: AppSpec) -> list[VmView]:
        if self._compute is None:
            return []
        views = []
        for rec in await self._compute.instance_states(app.name):
            phase, workers = None, 0
            if self._store is not None:
                try:
                    hb = json.loads(await self._store.get(heartbeat_key(app.name, rec.id, root=self._root)))
                    phase, workers = hb.get("phase"), int(hb.get("workers", 0))
                    # Gateway-stamped receive-time (absent on pre-ADR-012 VMs); drives hung detection.
                    rec.last_heartbeat = hb.get("received_at")
                except KeyNotFound:
                    pass
            views.append(VmView(record=rec, phase=phase, workers_running=workers))
        return views

    def _target_clusters(self, app: AppSpec) -> list[str]:
        """Registered clusters this app's k8s placement targets, in placement order (deduped)."""
        seen: set[str] = set()
        out: list[str] = []
        for c in expand_candidates(app):
            if c.type == "kubernetes" and c.cluster not in seen:
                seen.add(c.cluster)
                if c.cluster in self._clusters:
                    out.append(c.cluster)
        return out

    async def reconcile_one(self, app: AppSpec) -> None:
        with RECONCILE_SECONDS.time():
            pods: list = []
            pod_cluster: dict[str, str] = {}
            for name in self._target_clusters(app):
                _pm, inspector = self._clusters[name]
                cluster_pods = await inspector.workers(app)
                for w in cluster_pods:
                    pod_cluster[w.pod] = name
                pods.extend(cluster_pods)
            try:
                vms = await self._observe_vms(app)
            except Exception:  # noqa: BLE001 - log to the ledger, then re-raise so the cycle backs off and reaps NOTHING
                await self._track_prober_failure(app)
                raise
            depth = await self._q.depth(app.infer_queue_ref)
            keys = [candidate_key(c) for c in expand_candidates(app)]
            stocked = await self._board.view(keys) if self._board else {}
            # A candidate targeting an unregistered cluster is unavailable — treat it as stocked so
            # the walk advances to the next candidate (and surfaces it in the Held reason) instead of
            # picking it and silently sitting in Scaling forever.
            for c in expand_candidates(app):
                if c.type == "kubernetes" and c.cluster not in self._clusters:
                    stocked.setdefault(candidate_key(c), f"cluster '{c.cluster}' not registered")
            now = time.time()
            self._classify_hung(app, vms, now)  # flag unreachable/wedged BEFORE plan() so they're excluded
            await self._gc_heartbeats(app, {v.record.id for v in vms})  # drop heartbeat.json of gone VMs

            # Batch-aware dual-lane scaling: when a batch block is present, observe the batch
            # queue and compute desired via the SLA formula.  Non-batch apps are untouched.
            # NOTE: batch_remaining ≈ pending file count (visible items in the batch queue) is a
            # v1 approximation.  With a 24 h SLA the batch_need is ~1 unless the file count is
            # very large, so batch ensures ≥1 instance without driving the count excessively.
            desired_override: int | None = None
            if app.batch is not None:
                batch_depth = await self._q.depth(app.batch_queue_ref)
                desired_override = sla_desired(
                    infer_visible=depth.visible,
                    batch_remaining=batch_depth.visible,
                    rate_per_min=app.scaling.rate_per_instance_per_min,
                    infer_sla_min=app.scaling.infer_sla_minutes,
                    batch_sla_hr=app.batch.batch_sla_hours,
                    max_instances=app.scaling.max_instances,
                )

            # Scale-down stabilization: feed plan() the held desired peak as a floor while the window
            # is open, so a transient queue drain doesn't immediately tear units down (anti-flap).
            peak_value, peak_until = self._desired_peak.get(app.name, (0.0, 0.0))
            desired_floor = int(peak_value) if now < peak_until else 0

            result = plan(
                app,
                Observed(pods=pods, vms=vms, depth=depth),
                stocked=stocked,
                now=now,
                cooldown_until=self._cooldown_until.get(app.name, 0.0),
                desired_floor=desired_floor,
                desired_override=desired_override,
            )
            # Refresh the peak: a rise (or an elapsed window) re-arms it for scale_down_stabilization_s;
            # a dip inside the window leaves the higher held value untouched.
            if result.desired > peak_value or now >= peak_until:
                self._desired_peak[app.name] = (float(result.desired), now + app.scaling.scale_down_stabilization_s)
            await self._execute(app, result, now, pod_cluster, {v.record.id: v.record for v in vms})
            await self._escalate_hung(app, vms, now)  # reset → delete a hung VM so it never leaks forever
            for state, n in _summarize(pods).items():
                WORKERS.labels(app=app.name, state=state).set(n)
            vm_states: dict[str, int] = {}
            for v in vms:
                vm_states[v.record.state.value] = vm_states.get(v.record.state.value, 0) + 1
            for state, n in vm_states.items():
                VMS.labels(app=app.name, state=state).set(n)
            self._log_status_line(app.name, pods, vms)
            if result.phase == "Held":
                HOLDS.labels(app=app.name).inc()
            status = AppStatus(
                phase=result.phase,
                reason=result.reason,
                candidate=result.candidate,
                workers={**_summarize(pods), **{f"vm_{k}": v for k, v in vm_states.items()}},
                queue=depth,
                updated_at=time.time(),  # readers (console/CLI) show "as of T" / flag stale
            )
            await self._reg.write_status(app.name, status)

    async def _mark(self, key: str, reason: str) -> None:
        if self._board is None:
            return
        await self._board.mark(key, reason)
        parts = key.split("/")
        STOCKOUTS.labels(substrate=parts[0], pricing=parts[-1]).inc()

    # --- VM-tracking ledger (ADR-012) — durable record + error log; the prober stays the truth. ---
    async def _track_upsert(self, record: VmRecord) -> None:
        if self._tracker is not None:
            await self._tracker.upsert(
                record.app, record.region, name=record.id, state=record.state.value, created_at=record.created_at
            )

    async def _track_error(self, app: str, region: str, name: str, error: str) -> None:
        if self._tracker is not None:
            await self._tracker.log_error(app, region, name, error)

    async def _track_prober_failure(self, app: AppSpec) -> None:
        # Prober errors aren't region-specific (one aggregated query per app), so log to every region
        # the app's VM candidates target — an operator inspecting any region's ledger sees the blip.
        if self._tracker is None:
            return
        for region in {c.location for c in expand_candidates(app) if c.type == "vm"}:
            await self._tracker.log_error(app.name, region, "", "prober failure (observe skipped; no reap this cycle)")

    # --- On-change per-app status line --------------------------------------------------------------
    def _log_status_line(self, app: str, pods: list, vms: list[VmView]) -> None:
        """Emit `app=<n> Healthy:H Starting:S Draining:D Unreachable:U` ONLY when the counts change.
        Health is derived from both substrates (pods + VMs); the reconcile loop runs every ~15s, so a
        steady fleet stays silent and a real shift in the fleet is the only thing that prints."""
        healthy = starting = draining = unreachable = 0
        for w in pods:
            if w.state == WorkerState.running:
                healthy += 1
            elif w.state in (WorkerState.starting, WorkerState.pending):
                starting += 1
            elif w.state == WorkerState.exited:
                draining += 1
        for v in vms:
            if v.unreachable or v.wedged:
                unreachable += 1
            elif v.record.state == VmState.running:
                healthy += 1
            elif v.record.state in (VmState.provisioning, VmState.booting):
                starting += 1
            elif v.record.state in (VmState.stopped, VmState.preempted):
                draining += 1
        counts = (healthy, starting, draining, unreachable)
        if self._last_status.get(app) == counts:
            return
        self._last_status[app] = counts
        logger.info(
            "app=%s Healthy:%d Starting:%d Draining:%d Unreachable:%d", app, healthy, starting, draining, unreachable
        )

    # --- Hung-VM detection + escalation (ADR-012) ---------------------------------------------------
    def _classify_hung(self, app: AppSpec, vms: list[VmView], now: float) -> None:
        """Flag RUNNING-but-hung VMs so plan() excludes them (→ a replacement is provisioned): a stale
        gateway heartbeat ⇒ `unreachable` (silent hang); workers_exited past the restart cap ⇒ `wedged`
        (likely OOM/misconfig). No I/O here — the reset/delete escalation is `_escalate_hung`."""
        stale_s = app.scaling.vm_heartbeat_stale_seconds
        alive = {v.record.id for v in vms}
        for v in vms:
            rec = v.record
            if rec.state == VmState.running and rec.last_heartbeat is not None and now - rec.last_heartbeat > stale_s:
                v.unreachable = True
                if rec.id not in self._unreachable_seen:
                    self._unreachable_seen.add(rec.id)
                    logger.warning(
                        "app=%s vm=%s UNREACHABLE (no heartbeat %.0fs) — excluded from capacity; replacing",
                        app.name,
                        rec.id,
                        now - rec.last_heartbeat,
                    )
            else:
                self._unreachable_seen.discard(rec.id)
            if v.phase == "workers_exited":
                self._exited_cycles[rec.id] = self._exited_cycles.get(rec.id, 0) + 1
                if self._exited_cycles[rec.id] > app.scaling.wedged_restart_max:
                    v.wedged = True
                    if self._exited_cycles[rec.id] == app.scaling.wedged_restart_max + 1:  # log the transition once
                        logger.warning(
                            "app=%s vm=%s WEDGED (workers exited %d cycles — probable OOM/misconfig); fix the spec",
                            app.name,
                            rec.id,
                            self._exited_cycles[rec.id],
                        )
            else:
                self._exited_cycles.pop(rec.id, None)
        # Prune tracking for VMs no longer observed (deleted/gone) so the dicts stay bounded.
        for d in (self._exited_cycles, self._reset_at):
            for gone in [k for k in d if k not in alive]:
                del d[gone]
        self._unreachable_seen &= alive

    async def _escalate_hung(self, app: AppSpec, vms: list[VmView], now: float) -> None:
        """Recover or reclaim an unreachable VM so it never holds a GPU forever: reset after
        `vm_reset_after_seconds`, delete after `vm_delete_after_seconds`. Safe to delete a RUNNING VM
        here because the heartbeat shares the broker work channel (can't heartbeat ⟺ can't work) and the
        prober still affirmatively reports it — independent signals (ADR-012)."""
        if self._compute is None:
            return
        for v in vms:
            if not v.unreachable or v.record.last_heartbeat is None:
                continue
            stale = now - v.record.last_heartbeat
            rid = v.record.id
            if stale > app.scaling.vm_delete_after_seconds:
                logger.warning("app=%s vm=%s hung %.0fs — DELETING (reset did not recover it)", app.name, rid, stale)
                await self._compute.delete_instance(v.record)
                if self._tracker is not None:
                    await self._tracker.mark_gone(app.name, v.record.region, rid)
                self._reset_at.pop(rid, None)
            elif stale > app.scaling.vm_reset_after_seconds and rid not in self._reset_at:
                logger.warning("app=%s vm=%s hung %.0fs — RESETTING (reboot)", app.name, rid, stale)
                await self._compute.reset_instance(v.record)
                self._reset_at[rid] = now
                await self._track_error(app.name, v.record.region, rid, f"unreachable {stale:.0f}s — reset issued")

    async def _gc_heartbeats(self, app: AppSpec, alive: set[str]) -> None:
        """Delete `…/vms/{vm}/heartbeat.json` for VMs the prober no longer reports (gone), so stale
        heartbeats don't accumulate in the state store. Only touches heartbeat docs — never the tracking
        ledger (a different filename) nor a live VM's heartbeat (it is re-written every poll)."""
        if self._store is None:
            return
        prefix = f"{self._root}/apps/{app.name}/vms/"
        for key in await self._store.list_keys(prefix):
            if not key.endswith("/heartbeat.json"):
                continue
            vm_id = key[len(prefix) :].split("/", 1)[0]
            if vm_id not in alive:
                await self._store.delete(key)

    async def _execute(
        self,
        app: AppSpec,
        result: PlacementPlan,
        now: float,
        pod_cluster: dict[str, str],
        vm_by_id: dict[str, VmRecord],
    ) -> None:
        # Route pod deletes to the cluster each pod lives in (default: in-cluster).
        to_delete = [a.pod for a in result.actions if isinstance(a, ReapPod | RemoveStuckPod)]
        by_cluster: dict[str, list[str]] = {}
        for name in to_delete:
            by_cluster.setdefault(pod_cluster.get(name, IN_CLUSTER), []).append(name)
        for cname, names in by_cluster.items():
            handle = self._clusters.get(cname)
            if handle is not None:
                await handle[0].delete_pods(app, names)
        for a in result.actions:
            if isinstance(a, MarkStockout):
                await self._mark(a.candidate_key, a.reason)
            elif isinstance(a, CreatePods):
                handle = self._clusters.get(a.candidate.cluster)
                if handle is None:
                    continue  # cluster not registered in AUTOSCALER__CLUSTERS — can't place here
                c = a.candidate
                await handle[0].create_pods(
                    app,
                    a.count,
                    selector=c.selector,
                    candidate_key=candidate_key(c),
                    tolerations=c.tolerations,
                    image=c.image,
                    env=c.env,
                    args=c.args,
                    instances=c.instances,
                    worker_type=c.worker_type,
                    server=c.server,
                )
                SCALE_UP_PODS.labels(app=app.name).inc(a.count)
                self._cooldown_until[app.name] = now + app.scaling.scale_up_cooldown_s
            elif isinstance(a, ProvisionVms) and self._compute is not None:
                c = a.candidate
                logger.info(
                    "app=%s provisioning %d vm(s) via %s %s/%s (gpu=%s pricing=%s instances=%s)",
                    app.name,
                    a.count,
                    c.cluster,
                    c.location,
                    c.machine_type,
                    c.accelerator_type,
                    c.pricing,
                    c.instances,
                )
                try:
                    created = await self._compute.provision(
                        app,
                        region=c.location,
                        pricing=c.pricing,
                        count=a.count,
                        candidate=c,  # the SELECTED candidate drives the render (machine/provider/image/env)
                        instances=c.instances,
                        args=c.args,
                        worker_type=c.worker_type,
                        server=c.server,
                    )
                except ProvisionFailure as e:
                    # No VM launched: mark the region stocked out (TTL keeps us off it) but do NOT arm
                    # the cooldown, so the next reconcile fails over to the next region on the very next
                    # loop instead of burning a debounce cycle. The cooldown below is a post-scale-up
                    # debounce only — it exists to let the prober observe a VM we DID launch.
                    logger.warning("app=%s provision FAILED kind=%s: %s", app.name, e.kind.value, str(e)[:1500])
                    await self._mark(candidate_key(a.candidate), f"{e.kind.value}: {e}")
                    await self._track_error(app.name, c.location, "", f"provision {e.kind.value}: {e}")
                else:
                    logger.info("app=%s provision OK (%d vm[s] requested)", app.name, a.count)
                    self._cooldown_until[app.name] = now + app.scaling.scale_up_cooldown_s
                    for r in created:
                        await self._track_upsert(r)  # record what we provisioned in the ledger
            elif isinstance(a, CommandVm) and self._commander is not None:
                await self._commander.command(app.name, a.vm_id, a.command)
            elif isinstance(a, DestroyVm) and self._compute is not None:
                # Stateless reap: direct cloud-API delete-by-name (the prober already confirmed the
                # instance is STOPPED/preempted, the only states plan() emits a DestroyVm for). The
                # record carries the zone needed to address it. Skip if it vanished from the observed set.
                rec = vm_by_id.get(a.vm_id)
                if rec is not None:
                    logger.info(
                        "app=%s vm=%s REAPED (%s in %s) — cloud-confirmed terminal",
                        app.name,
                        rec.id,
                        rec.state.value,
                        rec.region,
                    )
                    await self._compute.delete_instance(rec)
                    if self._tracker is not None:
                        await self._tracker.mark_gone(app.name, rec.region, rec.id)
