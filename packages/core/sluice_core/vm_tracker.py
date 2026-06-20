"""Per-(app, region) VM-tracking ledger over the control-plane state store.

A durable record of the burst VMs Sluice provisioned, their last-seen status, and
provision/preemption/prober errors. This is **not** terraform state — a plain JSON list + a capped
event log, with no diff/plan/destroy/lock semantics. The **cloud prober remains the source of truth**
for liveness (ADR-012); the ledger only adds resilience to prober blips and a place to log errors for
debugging. Reconcile never sizes the fleet from the ledger — it counts the prober's running set.

Key: ``{root}/apps/{app}/vms/{region}/tracking.json`` (state bucket, alongside heartbeats).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .errors import KeyNotFound
from .interfaces import ObjectStore

_EVENTS_CAP = 50  # keep the most recent N events; the ledger is a debugging aid, not an audit store


class VmLedgerEntry(BaseModel):
    name: str
    state: str = "provisioning"
    created_at: float = 0.0
    last_error: str | None = None


class VmLedgerEvent(BaseModel):
    name: str
    error: str


class VmLedger(BaseModel):
    vms: dict[str, VmLedgerEntry] = Field(default_factory=dict)
    events: list[VmLedgerEvent] = Field(default_factory=list)


class VmTracker:
    def __init__(self, store: ObjectStore, *, root: str = "sluice") -> None:
        self._store = store
        self._root = root

    def _key(self, app: str, region: str) -> str:
        return f"{self._root}/apps/{app}/vms/{region}/tracking.json"

    async def _load(self, app: str, region: str) -> VmLedger:
        try:
            raw = await self._store.get(self._key(app, region))
        except KeyNotFound:
            return VmLedger()
        return VmLedger.model_validate_json(raw)

    async def _save(self, app: str, region: str, ledger: VmLedger) -> None:
        await self._store.put(
            self._key(app, region),
            ledger.model_dump_json().encode(),
            content_type="application/json",
        )

    async def entries(self, app: str, region: str) -> list[VmLedgerEntry]:
        return list((await self._load(app, region)).vms.values())

    async def events(self, app: str, region: str) -> list[VmLedgerEvent]:
        return (await self._load(app, region)).events

    async def upsert(self, app: str, region: str, *, name: str, state: str, created_at: float) -> None:
        ledger = await self._load(app, region)
        existing = ledger.vms.get(name)
        ledger.vms[name] = VmLedgerEntry(
            name=name,
            state=state,
            created_at=created_at,
            last_error=existing.last_error if existing else None,
        )
        await self._save(app, region, ledger)

    async def mark_gone(self, app: str, region: str, name: str) -> None:
        ledger = await self._load(app, region)
        if name in ledger.vms:
            del ledger.vms[name]
            self._append_event(ledger, VmLedgerEvent(name=name, error="gone"))
            await self._save(app, region, ledger)

    async def log_error(self, app: str, region: str, name: str, error: str) -> None:
        ledger = await self._load(app, region)
        entry = ledger.vms.get(name)
        if entry is not None:
            entry.last_error = error
        self._append_event(ledger, VmLedgerEvent(name=name, error=error))
        await self._save(app, region, ledger)

    @staticmethod
    def _append_event(ledger: VmLedger, event: VmLedgerEvent) -> None:
        ledger.events.append(event)
        if len(ledger.events) > _EVENTS_CAP:
            del ledger.events[: len(ledger.events) - _EVENTS_CAP]
