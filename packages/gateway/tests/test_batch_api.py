import httpx
from fastapi import FastAPI
from sluice_core.batch_objects import BatchObjects
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.models import AppSpec, BatchSpec
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_gateway.app import build_app
from sluice_gateway.batch import build_batch_router


class _FakeRegistry:
    """Minimal AppRegistry stand-in: only get_app is exercised by the upload-url route."""

    def __init__(self, apps: dict[str, AppSpec]):
        self._apps = apps

    async def get_app(self, name: str) -> AppSpec | None:
        return self._apps.get(name)


def _client(registry: object | None = None):
    store = FakeObjectStore()
    q = FakeQueue()
    app = FastAPI()
    app.include_router(
        build_batch_router(
            queue=q,
            batch_objects=BatchObjects(store=store),
            registry=registry,
            url_ttl_s=900,
            now=lambda: 1000.0,
        )
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), store, q


async def test_should_enqueue_one_message_per_file_when_batch_submitted():
    c, store, q = _client()
    async with c:
        job = (await c.post("/v1/sam3/batch")).json()
        jid = job["job_id"]
        u = (await c.post(f"/v1/sam3/batch/{jid}/upload-url", json={"filename": "a.jsonl"})).json()
        assert "url" in u  # presigned PUT returned
        # simulate the client uploading the file directly to the store
        await store.put(f"AppData/sam3/batch/{jid}/input/a.jsonl", b'{"_rid":"r1"}\n')
        r = await c.post(f"/v1/sam3/batch/{jid}/submit", json={"files": ["a.jsonl"]})
        assert r.status_code == 200
    assert (await q.depth("sam3-batch")).visible == 1  # one message for the one file


async def test_should_reject_when_submitted_file_missing():
    c, store, q = _client()
    async with c:
        jid = (await c.post("/v1/sam3/batch")).json()["job_id"]
        r = await c.post(f"/v1/sam3/batch/{jid}/submit", json={"files": ["ghost.jsonl"]})
        assert r.status_code == 400
    assert (await q.depth("sam3-batch")).visible == 0


async def test_should_reject_traversal_filename_when_requesting_upload_url():
    c, store, _q = _client()
    async with c:
        jid = (await c.post("/v1/sam3/batch")).json()["job_id"]
        for bad in ("../escape.jsonl", "a/b.jsonl", "", ".hidden"):
            r = await c.post(f"/v1/sam3/batch/{jid}/upload-url", json={"filename": bad})
            assert r.status_code == 400, f"{bad!r} should be rejected, got {r.status_code}"
    # no key was ever minted for a bad name
    assert store._data == {f"AppData/sam3/batch/{jid}/manifest.json"} or all(
        "escape" not in k and "/b.jsonl" not in k for k in store._data
    )


async def test_should_reject_traversal_filename_when_submitting():
    c, _store, q = _client()
    async with c:
        jid = (await c.post("/v1/sam3/batch")).json()["job_id"]
        r = await c.post(f"/v1/sam3/batch/{jid}/submit", json={"files": ["../escape.jsonl"]})
        assert r.status_code == 400
    assert (await q.depth("sam3-batch")).visible == 0


async def test_should_404_and_not_enqueue_when_submitting_unknown_job():
    c, store, q = _client()
    async with c:
        # Seed an input file under an unknown job so the file-exists check would pass,
        # proving the manifest 404 fires BEFORE any enqueue (M2 ordering).
        await store.put("AppData/sam3/batch/ghost/input/a.jsonl", b'{"_rid":"r1"}\n')
        r = await c.post("/v1/sam3/batch/ghost/submit", json={"files": ["a.jsonl"]})
        assert r.status_code == 404
    assert (await q.depth("sam3-batch")).visible == 0  # nothing enqueued before validation


async def test_should_return_presigned_get_urls_for_gzipped_output_parts():
    c, store, _q = _client()
    async with c:
        jid = (await c.post("/v1/sam3/batch")).json()["job_id"]
        # two gzipped output parts as the worker would have written them (.jsonl.gz)
        await store.put(f"AppData/sam3/batch/{jid}/output/a.jsonl.part-000000000.jsonl.gz", b"gz0")
        await store.put(f"AppData/sam3/batch/{jid}/output/a.jsonl.part-000000050.jsonl.gz", b"gz1")
        r = await c.get(f"/v1/sam3/batch/{jid}/output")
    assert r.status_code == 200
    parts = r.json()["parts"]
    assert len(parts) == 2
    assert all(p["key"].endswith(".jsonl.gz") for p in parts)  # client sees the .gz marker
    assert all(p["url"].startswith("memory://GET/") for p in parts)  # presigned GET per part


async def test_should_use_per_app_upload_ttl_hours_for_presign_expiry_when_registry_resolves_batch():
    # App declares uploadTtlHours=2 ⇒ presign expiry must be 2*3600=7200s, NOT the global 900s.
    spec = AppSpec(name="sam3", image="i", handler="h:H", batch=BatchSpec(upload_ttl_hours=2))
    c, _store, _q = _client(registry=_FakeRegistry({"sam3": spec}))
    async with c:
        jid = (await c.post("/v1/sam3/batch")).json()["job_id"]
        u = (await c.post(f"/v1/sam3/batch/{jid}/upload-url", json={"filename": "a.jsonl"})).json()
    assert u["url"].endswith("?exp=7200"), u["url"]  # per-app TTL drove the presign, not url_ttl_s=900


async def test_should_fall_back_to_global_url_ttl_when_app_has_no_batch_block():
    # App resolves but has no batch block ⇒ presign expiry falls back to the global url_ttl_s=900.
    spec = AppSpec(name="sam3", image="i", handler="h:H")
    c, _store, _q = _client(registry=_FakeRegistry({"sam3": spec}))
    async with c:
        jid = (await c.post("/v1/sam3/batch")).json()["job_id"]
        u = (await c.post(f"/v1/sam3/batch/{jid}/upload-url", json={"filename": "a.jsonl"})).json()
    assert u["url"].endswith("?exp=900"), u["url"]


async def test_should_reach_new_batch_routes_when_mounted_via_build_app():
    store = FakeObjectStore()
    app = build_app(
        queue=FakeQueue(),
        objects=ObjectStoreInferenceObjects(store=store),
        batch_objects=BatchObjects(store=store),
        t_sync_s=0,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/sam3/batch")
    assert r.status_code == 200
    assert "job_id" in r.json()  # new create-job route reachable, not the old shape
