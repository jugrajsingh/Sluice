from __future__ import annotations

from sluice_core.config import Settings
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_drivers.factory import build_object_store, build_queue

from .app import build_app

_s = Settings()
app = build_app(queue=build_queue(_s), objects=ObjectStoreInferenceObjects(store=build_object_store(_s)))
# uvicorn sluice_gateway.main:app
