"""Wiring test for the gateway entrypoint: the data/control store split (ADR-011).

`sluice_gateway.main` must feed the DATA store to inference + batch objects and the STATE store
to the VM control channel. We monkeypatch the factory builders + `build_app` to sentinels and
re-import the module, then assert which store reached which consumer.
"""

from __future__ import annotations

import importlib
import sys

from sluice_core.testing.fakes import FakeQueue


def test_main_routes_data_to_objects_and_state_to_vm(monkeypatch):
    import sluice_drivers.factory as factory
    import sluice_gateway.app as gw_app

    data = object()  # sentinel DATA store
    state = object()  # sentinel STATE store
    monkeypatch.setattr(factory, "build_object_store", lambda s: data)
    monkeypatch.setattr(factory, "build_state_store", lambda s: state)
    monkeypatch.setattr(factory, "build_queue", lambda s, **kw: FakeQueue())

    captured: dict = {}
    monkeypatch.setattr(gw_app, "build_app", lambda **kw: (captured.update(kw), object())[1])

    sys.modules.pop("sluice_gateway.main", None)
    try:
        importlib.import_module("sluice_gateway.main")
        # inference + batch artefacts → DATA store; VM heartbeat/command channel → STATE store.
        assert captured["objects"]._store is data
        assert captured["batch_objects"]._store is data
        assert captured["vm_objects"]._store is state
    finally:
        sys.modules.pop("sluice_gateway.main", None)  # don't leave the fake-app module cached
