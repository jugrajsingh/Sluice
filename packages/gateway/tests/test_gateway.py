import asyncio

import httpx
from sluice_core.compression import gzip_if_smaller, is_gzip
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_gateway.app import _result_response, build_app
from sluice_gateway.util import content_hash

_BIG = b'{"mask":[' + b"false, " * 50_000 + b"]}"  # highly redundant mask JSON -> gzips ~350x


def _stack():
    return FakeQueue(), ObjectStoreInferenceObjects(store=FakeObjectStore())


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _worker_once(q, objs, model):
    msgs = await q.receive(f"{model}-infer", max_messages=10, wait_seconds=1)
    for m in msgs:
        rid = m.body.decode()
        body = await objs.get_request(model, rid)
        await objs.put_result(model, rid, b"MASKS:" + body)
        await q.ack(f"{model}-infer", m)


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
    assert (await q.depth("topwear-infer")).visible == 1  # job enqueued on the infer lane


async def test_queue_carries_only_the_request_id():
    q, objs = _stack()
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        await c.post("/v1/topwear/infer", content=b"img")
    msg = (await q.receive("topwear-infer", max_messages=1, wait_seconds=1))[0]
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


def test_result_response_passes_gzip_through_when_client_accepts():
    stored = gzip_if_smaller(_BIG)
    assert is_gzip(stored)  # precondition: large body was compressed
    r = _result_response(stored, "gzip, deflate")
    assert r.headers["content-encoding"] == "gzip"  # advertised
    assert r.body == stored  # served compressed verbatim (small wire)


def test_result_response_gunzips_when_client_does_not_accept_gzip():
    stored = gzip_if_smaller(_BIG)
    r = _result_response(stored, "identity")
    assert "content-encoding" not in r.headers  # no gzip advertised
    assert r.body == _BIG  # decompressed for the non-negotiating client


def test_result_response_serves_raw_body_unchanged():
    r = _result_response(b'{"results":[]}', "gzip")  # not gzipped (no magic header)
    assert "content-encoding" not in r.headers
    assert r.body == b'{"results":[]}'


async def test_large_gzipped_result_round_trips_end_to_end():
    q, objs = _stack()
    rid = content_hash(b"img")
    await objs.put_result("topwear", rid, gzip_if_smaller(_BIG))  # as the adapter would store it
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        r = await c.get(f"/v1/topwear/status/{rid}")  # httpx sends Accept-Encoding: gzip and auto-decompresses
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"  # gateway served it compressed
    assert r.content == _BIG  # client transparently recovered the verbatim result


async def test_should_key_dedupe_cache_by_rid_when_present():
    q, objs = _stack()
    # a result pre-stored under the _rid is served on a cache hit keyed by _rid (NOT the body hash)
    await objs.put_result("topwear", "k1", b"CACHED")
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        hit = await c.post("/v1/topwear/infer", content=b'{"_rid":"k1","img":"anything"}')
        miss = await c.post("/v1/topwear/infer", content=b'{"img":"no-rid-here"}')
    assert hit.status_code == 200 and hit.content == b"CACHED"  # cache lookup keyed by _rid
    assert miss.status_code == 202  # no _rid -> body-hash key -> not cached
