from fastapi.testclient import TestClient
from sluice_core.auth import mint_worker_token
from sluice_core.models import Message
from sluice_gateway.app import build_app

KEY = "broker-signing-key"  # gitleaks:allow (test fixture, not a secret)


class FakeQueue:
    def __init__(self):
        self.acked = []
        self.extended = []
        self.nacked = []
        self._first = True

    async def receive(self, source, *, max_messages, wait_seconds):
        if not self._first:
            return []
        self._first = False
        return [Message(id="1-0", body=b"rid1", ack_token="1-0")][:max_messages]

    async def ack(self, source, msg):
        self.acked.append((source, msg.ack_token))

    async def extend_lease(self, source, msg, seconds):
        self.extended.append((msg.ack_token, seconds))

    async def nack(self, source, msg):
        self.nacked.append(msg.ack_token)


class FakeObjects:
    async def signed_get_request(self, app, rid, *, expires_s):
        return f"https://s/get/{app}/{rid}"

    async def signed_put_result(self, app, rid, *, expires_s):
        return f"https://s/put/{app}/{rid}"


def _client(queue=None, objects=None):
    return TestClient(build_app(queue=queue or FakeQueue(), objects=objects or FakeObjects(), signing_key=KEY))


def _auth(app="seg"):
    return {"Authorization": f"Bearer {mint_worker_token(app=app, worker_id='w1', key=KEY)}"}


def test_lease_requires_token():
    assert _client().post("/internal/v1/lease", json={"max": 4}).status_code == 401


def test_lease_returns_signed_urls():
    r = _client().post("/internal/v1/lease", json={"max": 4}, headers=_auth())
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["request_id"] == "rid1" and item["lease_id"] == "1-0"
    assert item["body_url"].endswith("/get/seg/rid1")
    assert item["result_url"].endswith("/put/seg/rid1")


def test_ack_calls_queue():
    q = FakeQueue()
    c = _client(queue=q)
    assert c.post("/internal/v1/ack", json={"lease_id": "1-0"}, headers=_auth()).status_code == 200
    assert q.acked == [("seg-infer", "1-0")]  # infer lane queue


def test_extend_calls_queue():
    q = FakeQueue()
    c = _client(queue=q)
    assert c.post("/internal/v1/extend", json={"lease_ids": ["1-0", "2-0"]}, headers=_auth()).status_code == 200
    assert [t for t, _ in q.extended] == ["1-0", "2-0"]


def test_nack_calls_queue():
    q = FakeQueue()
    c = _client(queue=q)
    assert c.post("/internal/v1/nack", json={"lease_id": "1-0"}, headers=_auth()).status_code == 200
    assert q.nacked == ["1-0"]


def test_client_routes_still_work_without_signing_key():
    # broker disabled when no signing key; client API unaffected
    app = build_app(queue=FakeQueue(), objects=FakeObjects())
    assert TestClient(app).get("/healthz").json() == {"ok": True}
