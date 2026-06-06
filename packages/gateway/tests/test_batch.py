import httpx
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.drivers.memory import MemoryQueue
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_gateway.app import build_app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_batch_enqueues_all_and_reports(tmp_path):
    q = MemoryQueue()
    objs = ObjectStoreInferenceObjects(store=LocalObjectStore(root=str(tmp_path)))
    app = build_app(queue=q, objects=objs, t_sync_s=0)
    async with _client(app) as c:
        r = await c.post("/v1/topwear/batch", json={"inputs": ["aGVsbG8=", "d29ybGQ="]})  # b64
        bid = r.json()["batch_id"]
        assert r.status_code == 202
        assert (await q.depth("topwear")).visible == 2
        st = await c.get(f"/v1/topwear/batch/{bid}")
    assert st.status_code == 200 and "completed" in st.json()
