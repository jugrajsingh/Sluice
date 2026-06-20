from sluice_core.compression import gunzip
from sluice_worker.broker_client import TokenExpired
from sluice_worker.config import WorkerSettings
from sluice_worker.worker import Worker


class FakeBroker:
    def __init__(self, batches=None):
        self._batches = (
            batches
            if batches is not None
            else [[{"request_id": "r1", "lease_id": "1-0", "body_url": "u1", "result_url": "p1"}], []]
        )
        self.acked = []
        self.extended = []
        self.bodies = {"u1": b"in"}
        self.puts = {}
        self.closed = False

    async def lease(self, max):
        return self._batches.pop(0) if self._batches else []

    async def get(self, url):
        return self.bodies.get(url, b"in")

    async def put(self, url, data):
        self.puts[url] = data

    async def ack(self, lease_id):
        self.acked.append(lease_id)

    async def extend(self, lease_ids):
        self.extended.append(list(lease_ids))

    async def aclose(self):
        self.closed = True


class EchoHandler:
    async def load(self): ...

    async def predict(self, batch):
        return [b"out" for _ in batch]

    async def health(self):
        return True


def _settings(**kw):
    kw.setdefault("max_jobs", 1000)
    return WorkerSettings(app="app", batch_size=4, max_blank_retries=1, heartbeat_s=50, **kw)


async def test_worker_leases_processes_acks_then_exits():
    br = FakeBroker()
    await Worker(broker=br, handler=EchoHandler(), settings=_settings()).run()
    assert gunzip(br.puts["p1"]) == b"out"  # results are always gzipped (.gz key)
    assert br.acked == ["1-0"]
    assert br.closed is True  # client closed on exit


async def test_worker_exits_on_token_expired():
    class ExpiringBroker(FakeBroker):
        async def lease(self, max):
            raise TokenExpired("expired")

    await Worker(broker=ExpiringBroker(), handler=EchoHandler(), settings=_settings()).run()  # returns, no raise


async def test_worker_respects_max_jobs():
    batches = [
        [{"request_id": f"r{i}", "lease_id": f"{i}-0", "body_url": "u1", "result_url": f"p{i}"}] for i in range(10)
    ]
    br = FakeBroker(batches=batches)
    await Worker(broker=br, handler=EchoHandler(), settings=_settings(max_jobs=3)).run()
    assert len(br.acked) == 3
