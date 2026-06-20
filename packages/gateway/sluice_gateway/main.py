from __future__ import annotations

import os

from sluice_core.batch_objects import BatchObjects
from sluice_core.config import Settings
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.vm_objects import VmObjects
from sluice_drivers.factory import build_object_store, build_queue, build_registry, build_state_store

from .app import build_app

_s = Settings()
# Data store (inference results + batch parts, AppData/ prefix) vs control-plane state store
# (VM heartbeats + commands). state_store unset ⇒ inherit the data store (single-bucket, ADR-011).
_data = build_object_store(_s)
_state = build_state_store(_s)
# Batch files take minutes to process, so the {app}-batch queue gets a LONG idle-reclaim
# window (M3) derived from the batch lease visibility; the worker heartbeat extends it.
_BATCH_LEASE_VISIBILITY_S = int(os.environ.get("GATEWAY__BATCH_LEASE_VISIBILITY_S") or "900")
app = build_app(
    queue=build_queue(_s),
    objects=ObjectStoreInferenceObjects(store=_data),
    signing_key=os.environ.get("GATEWAY__SIGNING_KEY") or None,
    api_key=os.environ.get("GATEWAY__API_KEY") or None,
    batch_objects=BatchObjects(store=_data),
    batch_queue=build_queue(_s, idle_reclaim_ms=_BATCH_LEASE_VISIBILITY_S * 1000),
    batch_lease_visibility_s=_BATCH_LEASE_VISIBILITY_S,
    vm_objects=VmObjects(store=_state),
    # App specs live in the control-plane state store; the gateway reads per-app batch config
    # (e.g. uploadTtlHours) from the SAME registry the console/autoscaler use — see ADR-011.
    registry=build_registry(_s, store=_state),
)
# uvicorn sluice_gateway.main:app
