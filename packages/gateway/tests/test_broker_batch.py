"""Broker batch-lease endpoints (spec §5 / C1).

A VM batch worker cannot reach in-cluster Redis directly; it leases batch files
through the JWT broker exactly like the infer lane. These endpoints lease from the
``{app}-batch`` queue and return a presigned GET (``body_url``) over the file's
``input_key`` so the worker can fetch the JSONL without store credentials.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sluice_core.auth import mint_worker_token
from sluice_core.batch_objects import BatchObjects
from sluice_core.models import Message
from sluice_core.testing.fakes import FakeObjectStore
from sluice_gateway.broker import build_broker_router

KEY = "broker-signing-key"  # gitleaks:allow (test fixture, not a secret)


class _RecordingBatchQueue:
    """Records receive/ack/extend/nack against the {app}-batch source."""

    def __init__(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        self.received_from: list[str] = []
        self.acked: list[tuple[str, str]] = []
        self.extended: list[tuple[str, int]] = []
        self.nacked: list[str] = []

    async def receive(self, source: str, *, max_messages: int, wait_seconds: int) -> list[Message]:
        self.received_from.append(source)
        out = self._messages[:max_messages]
        self._messages = self._messages[max_messages:]
        return out

    async def ack(self, source: str, msg: Message) -> None:
        self.acked.append((source, msg.ack_token))

    async def extend_lease(self, source: str, msg: Message, seconds: int) -> None:
        self.extended.append((msg.ack_token, seconds))

    async def nack(self, source: str, msg: Message) -> None:
        self.nacked.append(msg.ack_token)


def _client(*, infer_queue, batch_queue, batch_objects, **kw):
    app = FastAPI()
    app.include_router(
        build_broker_router(
            queue=infer_queue,
            objects=_StubInferObjects(),
            signing_key=KEY,
            batch_queue=batch_queue,
            batch_objects=batch_objects,
            **kw,
        )
    )
    return TestClient(app)


class _StubInferObjects:
    async def signed_get_request(self, app, rid, *, expires_s):
        return f"https://s/get/{app}/{rid}"

    async def signed_put_result(self, app, rid, *, expires_s):
        return f"https://s/put/{app}/{rid}"


class _StubInferQueue:
    async def receive(self, source, *, max_messages, wait_seconds):
        return []

    async def ack(self, source, msg):
        pass

    async def extend_lease(self, source, msg, seconds):
        pass

    async def nack(self, source, msg):
        pass


def _auth(app="sam3"):
    return {"Authorization": f"Bearer {mint_worker_token(app=app, worker_id='w1', key=KEY)}"}


def _batch_msg(mid: str, job_id: str, file: str) -> Message:
    return Message(id=mid, body=b"", attributes={"job_id": job_id, "file": file}, ack_token=mid)


def test_should_require_token_when_leasing_batch():
    c = _client(infer_queue=_StubInferQueue(), batch_queue=_RecordingBatchQueue([]), batch_objects=_bo())
    assert c.post("/internal/v1/batch/lease", json={"max": 1}).status_code == 401


def _bo() -> BatchObjects:
    return BatchObjects(store=FakeObjectStore())


def test_should_lease_batch_file_with_presigned_input_get_when_authed():
    bq = _RecordingBatchQueue([_batch_msg("1-0", "J1", "a.jsonl")])
    c = _client(infer_queue=_StubInferQueue(), batch_queue=bq, batch_objects=_bo())
    r = c.post("/internal/v1/batch/lease", json={"max": 1}, headers=_auth())
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["lease_id"] == "1-0"
    assert item["job_id"] == "J1"
    assert item["file"] == "a.jsonl"
    assert item["body_url"] == "memory://GET/AppData/sam3/batch/J1/input/a.jsonl?exp=900"
    # leased from the {app}-batch source, derived from the JWT app claim
    assert bq.received_from == ["sam3-batch"]


def test_should_ack_batch_file_against_batch_source_when_authed():
    bq = _RecordingBatchQueue([])
    c = _client(infer_queue=_StubInferQueue(), batch_queue=bq, batch_objects=_bo())
    assert c.post("/internal/v1/batch/ack", json={"lease_id": "1-0"}, headers=_auth()).status_code == 200
    assert bq.acked == [("sam3-batch", "1-0")]


def test_should_extend_batch_lease_with_long_ttl_when_authed():
    bq = _RecordingBatchQueue([])
    c = _client(infer_queue=_StubInferQueue(), batch_queue=bq, batch_objects=_bo(), batch_lease_visibility_s=900)
    assert c.post("/internal/v1/batch/extend", json={"lease_ids": ["1-0", "2-0"]}, headers=_auth()).status_code == 200
    assert [t for t, _ in bq.extended] == ["1-0", "2-0"]
    # the long batch visibility window is what is passed to extend_lease (M3)
    assert {s for _, s in bq.extended} == {900}


def test_should_nack_batch_file_when_authed():
    bq = _RecordingBatchQueue([])
    c = _client(infer_queue=_StubInferQueue(), batch_queue=bq, batch_objects=_bo())
    assert c.post("/internal/v1/batch/nack", json={"lease_id": "1-0"}, headers=_auth()).status_code == 200
    assert bq.nacked == ["1-0"]


def test_should_not_expose_batch_routes_when_batch_not_configured():
    """Without batch_queue/batch_objects the batch routes are absent (infer-only broker)."""
    app = FastAPI()
    app.include_router(build_broker_router(queue=_StubInferQueue(), objects=_StubInferObjects(), signing_key=KEY))
    c = TestClient(app)
    assert c.post("/internal/v1/batch/lease", json={"max": 1}, headers=_auth()).status_code == 404


def test_should_sign_output_part_put_for_claim_app_when_output_url():
    """A worker requests a presigned PUT for one output part; the key is derived from the JWT app."""
    c = _client(infer_queue=_StubInferQueue(), batch_queue=_RecordingBatchQueue([]), batch_objects=_bo())
    r = c.post(
        "/internal/v1/batch/output-url", json={"job_id": "J1", "file": "a.jsonl", "start_offset": 50}, headers=_auth()
    )
    assert r.status_code == 200
    assert r.json()["url"] == "memory://PUT/AppData/sam3/batch/J1/output/a.jsonl.part-000000050.jsonl.gz?exp=900"


def test_should_round_trip_status_when_post_then_get():
    bo = _bo()
    c = _client(infer_queue=_StubInferQueue(), batch_queue=_RecordingBatchQueue([]), batch_objects=bo)
    put = c.post(
        "/internal/v1/batch/status",
        json={
            "job_id": "J1",
            "file": "a.jsonl",
            "status": {
                "file": "a.jsonl",
                "state": "running",
                "records_total": 5,
                "records_done": 2,
                "records_failed": 0,
                "updated_at": 1.0,
            },
        },
        headers=_auth(),
    )
    assert put.status_code == 200
    g = c.get("/internal/v1/batch/status", params={"job_id": "J1", "file": "a.jsonl"}, headers=_auth())
    assert g.json()["found"] is True
    assert g.json()["status"]["records_done"] == 2


def test_should_return_not_found_when_status_absent():
    c = _client(infer_queue=_StubInferQueue(), batch_queue=_RecordingBatchQueue([]), batch_objects=_bo())
    g = c.get("/internal/v1/batch/status", params={"job_id": "J9", "file": "z.jsonl"}, headers=_auth())
    assert g.json() == {"found": False, "status": None}


def test_should_reject_bad_filename_when_output_url():
    c = _client(infer_queue=_StubInferQueue(), batch_queue=_RecordingBatchQueue([]), batch_objects=_bo())
    body = {"job_id": "J1", "file": "../etc/passwd", "start_offset": 0}
    r = c.post("/internal/v1/batch/output-url", json=body, headers=_auth())
    assert r.status_code == 400
