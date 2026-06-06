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
from sluice_core.models import AppSpec, AppStatus
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


class PodManager:
    """Bare-pod lifecycle on the k8s substrate."""

    async def create_pods(self, app: AppSpec, n: int, *, selector: dict[str, str], candidate_key: str = "") -> None: ...
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

    async def reconcile_one(self, app: AppSpec) -> None:
        with RECONCILE_SECONDS.time():
            pods = await self._inspect.workers(app)
            vms = await self._observe_vms(app)
            depth = await self._q.depth(app.queue_ref)
            keys = [candidate_key(c) for c in expand_candidates(app)]
            stocked = await self._board.view(keys) if self._board else {}
            now = time.time()
            result = plan(
                app,
                Observed(pods=pods, vms=vms, depth=depth),
                stocked=stocked,
                now=now,
                cooldown_until=self._cooldown_until.get(app.name, 0.0),
                boot_deadline_s=self._boot_deadline,
            )
            await self._execute(app, result, now)
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

    async def _execute(self, app: AppSpec, result: PlacementPlan, now: float) -> None:
        reap = [a.pod for a in result.actions if isinstance(a, ReapPod)]
        stuck = [a.pod for a in result.actions if isinstance(a, RemoveStuckPod)]
        if reap or stuck:
            await self._pods.delete_pods(app, reap + stuck)
        for a in result.actions:
            if isinstance(a, MarkStockout):
                await self._mark(a.candidate_key, a.reason)
            elif isinstance(a, CreatePods):
                await self._pods.create_pods(
                    app, a.count, selector=a.candidate.selector, candidate_key=candidate_key(a.candidate)
                )
                SCALE_UP_PODS.labels(app=app.name).inc(a.count)
                self._cooldown_until[app.name] = now + app.scaling.cooldown_s
            elif isinstance(a, ProvisionVms) and self._compute is not None:
                try:
                    await self._compute.provision(
                        app, region=a.candidate.location, pricing=a.candidate.pricing, count=a.count
                    )
                except ProvisionFailure as e:
                    await self._mark(candidate_key(a.candidate), f"{e.kind.value}: {e}")
                self._cooldown_until[app.name] = now + app.scaling.cooldown_s
            elif isinstance(a, CommandVm) and self._commander is not None:
                await self._commander.command(app.name, a.vm_id, a.command)
            elif isinstance(a, DestroyVm) and self._compute is not None:
                await self._compute.destroy(app.name, [a.vm_id])
