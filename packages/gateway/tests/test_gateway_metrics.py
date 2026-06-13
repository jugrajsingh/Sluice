import httpx
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_gateway.app import build_app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_healthz_and_metrics():
    objs = ObjectStoreInferenceObjects(store=FakeObjectStore())
    app = build_app(queue=FakeQueue(), objects=objs, t_sync_s=0)
    async with _client(app) as c:
        assert (await c.get("/healthz")).status_code == 200
        await c.post("/v1/m/infer", content=b"x")  # one enqueue
        m = await c.get("/metrics")
    assert m.status_code == 200
    assert 'sluice_gateway_enqueues_total{app="m"}' in m.text
