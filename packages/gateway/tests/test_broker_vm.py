"""Broker VM control-channel endpoints.

The VM agent holds no object-store credentials; it POSTs its heartbeat and GETs its command
through the JWT broker, which performs the tiny store writes (VmObjects) server-side. The app
the writes are scoped to comes from the JWT claim, never the request body.
"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sluice_core.auth import mint_worker_token
from sluice_gateway.broker import build_broker_router

KEY = "broker-signing-key"  # gitleaks:allow (test fixture, not a secret)


class _Vm:
    def __init__(self) -> None:
        self.beats: list[tuple[str, str, dict]] = []
        self.cmds: dict[tuple[str, str], str] = {}

    async def put_heartbeat(self, app, vm_id, doc):
        self.beats.append((app, vm_id, doc))

    async def pop_command(self, app, vm_id):
        return self.cmds.pop((app, vm_id), None)


class _StubInferQueue:
    async def receive(self, source, *, max_messages, wait_seconds):
        return []


class _StubInferObjects:
    async def signed_get_request(self, app, rid, *, expires_s):
        return "x"

    async def signed_put_result(self, app, rid, *, expires_s):
        return "x"


def _auth(app="app1"):
    return {"Authorization": f"Bearer {mint_worker_token(app=app, worker_id='w1', key=KEY)}"}


def _client(vm):
    app = FastAPI()
    app.include_router(
        build_broker_router(queue=_StubInferQueue(), objects=_StubInferObjects(), signing_key=KEY, vm_objects=vm)
    )
    return TestClient(app)


def test_should_record_heartbeat_under_claim_app_when_posted():
    vm = _Vm()
    c = _client(vm)
    r = c.post("/internal/v1/vm/heartbeat", json={"vm_id": "vm1", "phase": "running", "workers": 3}, headers=_auth())
    assert r.json() == {"ok": True}
    assert vm.beats[0][0] == "app1"
    assert vm.beats[0][2]["phase"] == "running"


def test_should_stamp_received_at_server_side_when_heartbeat_posted():
    # The gateway adds a trusted receive-time (the VM has no trusted clock); powers hung-VM detection.
    vm = _Vm()
    c = _client(vm)
    before = time.time()
    c.post("/internal/v1/vm/heartbeat", json={"vm_id": "vm1", "phase": "running", "workers": 1}, headers=_auth())
    doc = vm.beats[0][2]
    assert "received_at" in doc and doc["received_at"] >= before


def test_should_pop_command_when_get_vm_command():
    vm = _Vm()
    vm.cmds[("app1", "vm1")] = "shutdown"
    c = _client(vm)
    r = c.get("/internal/v1/vm/command", params={"vm_id": "vm1"}, headers=_auth())
    assert r.json() == {"command": "shutdown"}


def test_should_require_token_when_posting_heartbeat():
    c = _client(_Vm())
    r = c.post("/internal/v1/vm/heartbeat", json={"vm_id": "vm1", "phase": "running", "workers": 1})
    assert r.status_code == 401


def test_should_not_expose_vm_routes_when_vm_objects_absent():
    app = FastAPI()
    app.include_router(build_broker_router(queue=_StubInferQueue(), objects=_StubInferObjects(), signing_key=KEY))
    c = TestClient(app)
    assert c.get("/internal/v1/vm/command", params={"vm_id": "vm1"}, headers=_auth()).status_code == 404
