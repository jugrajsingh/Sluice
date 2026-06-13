import httpx
from sluice_worker.adapter import Adapter


class FakeBroker:
    def __init__(self, items, bodies):
        self._items = list(items)
        self._handed = False
        self.bodies = dict(bodies)
        self.puts: dict[str, bytes] = {}
        self.acked: list[str] = []
        self.nacked: list[str] = []

    async def lease(self, n):
        if self._handed:
            return []
        self._handed = True
        return self._items[:n]

    async def get(self, url):
        return self.bodies[url]

    async def put(self, url, data):
        self.puts[url] = data

    async def ack(self, lease_id):
        self.acked.append(lease_id)

    async def nack(self, lease_id):
        self.nacked.append(lease_id)


def _server(handler):
    return httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handler))


def _adapter(broker, server, **kw):
    return Adapter(
        broker=broker,
        server=server,
        request_path="/v1/segment",
        method="POST",
        content_type="application/json",
        concurrency=1,
        max_blank_retries=1,
        **kw,
    )


async def test_forwards_body_and_writes_response_verbatim():
    item = {"lease_id": "L1", "body_url": "https://s3/body", "result_url": "https://s3/result"}
    broker = FakeBroker([item], {"https://s3/body": b'{"inputs":[]}'})

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        seen["ct"] = request.headers.get("content-type")
        return httpx.Response(200, content=b'{"results":[]}')

    await _adapter(broker, _server(handler)).run()
    assert seen["path"] == "/v1/segment" and seen["body"] == b'{"inputs":[]}' and seen["ct"] == "application/json"
    assert broker.puts["https://s3/result"] == b'{"results":[]}'  # response stored verbatim
    assert broker.acked == ["L1"] and broker.nacked == []


async def test_nacks_on_server_5xx_without_writing_result():
    item = {"lease_id": "L2", "body_url": "https://s3/b", "result_url": "https://s3/r"}
    broker = FakeBroker([item], {"https://s3/b": b"x"})

    await _adapter(broker, _server(lambda r: httpx.Response(500, content=b"oom"))).run()
    assert broker.nacked == ["L2"] and broker.acked == []
    assert "https://s3/r" not in broker.puts


async def test_handle_error_nacks_and_does_not_raise():
    # a transport failure must not escape into the dispatch engine (which would leak the lease) —
    # the adapter nacks so the lease is retried.
    item = {"lease_id": "L3", "body_url": "https://s3/b", "result_url": "https://s3/r"}
    broker = FakeBroker([item], {})  # body_url missing -> broker.get raises KeyError

    await _adapter(broker, _server(lambda r: httpx.Response(200, content=b"x"))).run()
    assert broker.nacked == ["L3"] and broker.acked == []
    assert "https://s3/r" not in broker.puts


async def test_put_failure_nacks_once_and_does_not_ack():
    item = {"lease_id": "L4", "body_url": "https://s3/b", "result_url": "https://s3/r"}

    class PutFails(FakeBroker):
        async def put(self, url, data):
            raise RuntimeError("put boom")  # 2xx response, but storing the result fails

    broker = PutFails([item], {"https://s3/b": b"x"})
    await _adapter(broker, _server(lambda r: httpx.Response(200, content=b"y"))).run()
    assert broker.nacked == ["L4"] and broker.acked == []  # nacked exactly once, never acked


async def test_failing_nack_is_suppressed_and_not_retried():
    item = {"lease_id": "L5", "body_url": "https://s3/b", "result_url": "https://s3/r"}

    class NackFails(FakeBroker):
        async def nack(self, lease_id):
            self.nacked.append(lease_id)
            raise RuntimeError("nack boom")  # the nack itself fails

    broker = NackFails([item], {"https://s3/b": b"x"})
    await _adapter(broker, _server(lambda r: httpx.Response(500))).run()  # must not raise or double-nack
    assert broker.nacked == ["L5"]  # attempted once; the failure is suppressed (no second nack)


async def test_wait_ready_polls_until_healthy():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] >= 2 else 503)

    adapter = _adapter(FakeBroker([], {}), _server(handler), health_path="/healthz", ready_timeout_s=5, poll_s=0.0)
    assert await adapter.wait_ready() is True
    assert calls["n"] >= 2  # polled until the model server reported ready
