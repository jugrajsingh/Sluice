from __future__ import annotations

import json
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
from sluice_core.models import AppSpec, AppStatus, Toleration
from sluice_core.vm_paths import heartbeat_key

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
        stockout_ttl_s: int = 600,
        boot_deadline_s: int = 600,
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
        self._board = StockoutBoard(cache=cache, ttl_s=stockout_ttl_s) if cache else None
        self._boot_deadline = boot_deadline_s
        self._root = vm_root
        self._cooldown_until: dict[str, float] = {}

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
            vms = await self._observe_vms(app)
            depth = await self._q.depth(app.queue_ref)
            keys = [candidate_key(c) for c in expand_candidates(app)]
            stocked = await self._board.view(keys) if self._board else {}
            # A candidate targeting an unregistered cluster is unavailable — treat it as stocked so
            # the walk advances to the next candidate (and surfaces it in the Held reason) instead of
            # picking it and silently sitting in Scaling forever.
            for c in expand_candidates(app):
                if c.type == "kubernetes" and c.cluster not in self._clusters:
                    stocked.setdefault(candidate_key(c), f"cluster '{c.cluster}' not registered")
            now = time.time()
            result = plan(
                app,
                Observed(pods=pods, vms=vms, depth=depth),
                stocked=stocked,
                now=now,
                cooldown_until=self._cooldown_until.get(app.name, 0.0),
                boot_deadline_s=self._boot_deadline,
            )
            await self._execute(app, result, now, pod_cluster)
            for state, n in _summarize(pods).items():
                WORKERS.labels(app=app.name, state=state).set(n)
            vm_states: dict[str, int] = {}
            for v in vms:
                vm_states[v.record.state.value] = vm_states.get(v.record.state.value, 0) + 1
            for state, n in vm_states.items():
                VMS.labels(app=app.name, state=state).set(n)
            if result.phase == "Held":
                HOLDS.labels(app=app.name).inc()
            status = AppStatus(
                phase=result.phase,
                reason=result.reason,
                candidate=result.candidate,
                workers={**_summarize(pods), **{f"vm_{k}": v for k, v in vm_states.items()}},
                queue=depth,
            )
            await self._reg.write_status(app.name, status)

    async def _mark(self, key: str, reason: str) -> None:
        if self._board is None:
            return
        await self._board.mark(key, reason)
        parts = key.split("/")
        STOCKOUTS.labels(substrate=parts[0], pricing=parts[-1]).inc()

    async def _execute(self, app: AppSpec, result: PlacementPlan, now: float, pod_cluster: dict[str, str]) -> None:
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
                self._cooldown_until[app.name] = now + app.scaling.cooldown_s
            elif isinstance(a, ProvisionVms) and self._compute is not None:
                c = a.candidate
                try:
                    await self._compute.provision(
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
                    await self._mark(candidate_key(a.candidate), f"{e.kind.value}: {e}")
                self._cooldown_until[app.name] = now + app.scaling.cooldown_s
            elif isinstance(a, CommandVm) and self._commander is not None:
                await self._commander.command(app.name, a.vm_id, a.command)
            elif isinstance(a, DestroyVm) and self._compute is not None:
                await self._compute.destroy(app.name, [a.vm_id])
