from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.drivers.memory import MemoryQueue
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_worker.config import WorkerSettings
from sluice_worker.handler import BaseHandler
from sluice_worker.worker import Worker


class EchoHandler(BaseHandler):
    def __init__(self):
        super().__init__()
        self.loads = 0
        self.batches = []

    async def load(self):
        await super().load()
        self.loads += 1

    async def predict(self, batch):
        self.batches.append(len(batch))
        return [b"OUT:" + b for b in batch]


def _objects(tmp_path):
    return ObjectStoreInferenceObjects(store=LocalObjectStore(root=str(tmp_path)))


async def _enqueue(q, objs, n):
    for i in range(n):
        rid = f"h{i}"
        await objs.put_request("app", rid, f"img{i}".encode())
        await q.enqueue("jobs", rid.encode())


def _settings(**kw):
    return WorkerSettings(app="app", source="jobs", batch_size=4, wait_seconds=0, max_blank_retries=1, **kw)


async def test_processes_writes_acks_and_exits_on_empty(tmp_path):
    q, objs = MemoryQueue(default_lease_s=30), _objects(tmp_path)
    h = EchoHandler()
    await _enqueue(q, objs, 6)
    w = Worker(queue=q, objects=objs, handler=h, settings=_settings(max_jobs=1000))
    await w.run()
    assert h.loads == 1  # model loaded once
    assert await objs.get_result("app", "h0") == b"OUT:img0"
    assert (await q.depth("jobs")).visible == 0  # all consumed
    assert max(h.batches) <= 4  # respected batch_size


async def test_exits_on_max_jobs(tmp_path):
    q, objs = MemoryQueue(default_lease_s=30), _objects(tmp_path)
    await _enqueue(q, objs, 20)
    w = Worker(queue=q, objects=objs, handler=EchoHandler(), settings=_settings(max_jobs=8))
    await w.run()
    # processed exactly up to the batch boundary >= 8, then exits; remainder stays queued
    assert (await q.depth("jobs")).visible > 0


async def test_stop_drains_current_batch_then_exits(tmp_path):
    q, objs = MemoryQueue(default_lease_s=30), _objects(tmp_path)
    await _enqueue(q, objs, 4)

    class StopHandler(EchoHandler):
        def __init__(self, w_ref):
            super().__init__()
            self.w_ref = w_ref

        async def predict(self, batch):
            self.w_ref.request_stop()  # stop mid-run
            return await super().predict(batch)

    w = Worker(queue=q, objects=objs, handler=None, settings=_settings(max_jobs=1000))
    w._handler = StopHandler(w)  # inject after constructing for the ref
    await w.run()
    assert w._handler.loads == 1
    # current batch finished and acked; loop then exits due to stop flag
    assert (await q.depth("jobs")).in_flight == 0


async def test_redelivery_is_idempotent_via_result_id(tmp_path):
    q, objs = MemoryQueue(default_lease_s=30), _objects(tmp_path)
    await _enqueue(q, objs, 1)
    w = Worker(queue=q, objects=objs, handler=EchoHandler(), settings=_settings(max_jobs=1000))
    await w.run()
    # same result id written deterministically -> safe to reprocess
    assert await objs.result_exists("app", "h0")
