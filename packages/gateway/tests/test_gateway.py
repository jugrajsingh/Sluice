import asyncio

import httpx
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_gateway.app import build_app
from sluice_gateway.util import content_hash


def _stack():
    return FakeQueue(), ObjectStoreInferenceObjects(store=FakeObjectStore())


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _worker_once(q, objs, model):
    msgs = await q.receive(model, max_messages=10, wait_seconds=1)
    for m in msgs:
        rid = m.body.decode()
        body = await objs.get_request(model, rid)
        await objs.put_result(model, rid, b"MASKS:" + body)
        await q.ack(model, m)


async def test_cache_hit_returns_200():
    q, objs = _stack()
    await objs.put_result("topwear", content_hash(b"img"), b"CACHED")
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        r = await c.post("/v1/topwear/infer", content=b"img")
    assert r.status_code == 200 and r.content == b"CACHED"


async def test_miss_returns_202_with_ticket():
    q, objs = _stack()
    app = build_app(queue=q, objects=objs, t_sync_s=0)  # no warm worker, no wait
    async with _client(app) as c:
        r = await c.post("/v1/topwear/infer", content=b"img")
    assert r.status_code == 202
    body = r.json()
    assert "ticket" in body and "retry_after" in body
    assert (await q.depth("topwear")).visible == 1  # job enqueued


async def test_queue_carries_only_the_request_id():
    q, objs = _stack()
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        await c.post("/v1/topwear/infer", content=b"img")
    msg = (await q.receive("topwear", max_messages=1, wait_seconds=1))[0]
    assert msg.body.decode() == content_hash(b"img")
    assert await objs.get_request("topwear", msg.body.decode()) == b"img"


async def test_sync_sugar_returns_200_when_worker_fast():
    q, objs = _stack()
    app = build_app(queue=q, objects=objs, t_sync_s=2)
    async with _client(app) as c:
        task = asyncio.create_task(c.post("/v1/topwear/infer", content=b"img"))
        await asyncio.sleep(0.05)
        await _worker_once(q, objs, "topwear")  # worker finishes within t_sync
        r = await task
    assert r.status_code == 200 and r.content == b"MASKS:img"


async def test_status_returns_result_after_worker():
    q, objs = _stack()
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        sub = await c.post("/v1/topwear/infer", content=b"img")
        ticket = sub.json()["ticket"]
        pending = await c.get(f"/v1/topwear/status/{ticket}")
        assert pending.status_code == 202
        await _worker_once(q, objs, "topwear")
        done = await c.get(f"/v1/topwear/status/{ticket}")
    assert done.status_code == 200 and done.content == b"MASKS:img"
