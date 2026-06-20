import httpx
from sluice_core.auth import mint_worker_token
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_gateway.app import build_app

KEY = "test-signing-key"  # gitleaks:allow (test fixture, not a secret)
APIKEY = "test-api-key"  # gitleaks:allow (test fixture, not a secret)


def _app(api_key=APIKEY):
    return build_app(
        queue=FakeQueue(),
        objects=ObjectStoreInferenceObjects(store=FakeObjectStore()),
        t_sync_s=0,
        signing_key=KEY,
        api_key=api_key,
    )


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_infer_rejects_without_or_with_wrong_key():
    async with _client(_app()) as c:
        assert (await c.post("/v1/m/infer", content=b"x")).status_code == 401
        assert (await c.post("/v1/m/infer", content=b"x", headers={"X-API-Key": "nope"})).status_code == 401


async def test_infer_accepts_with_correct_key():
    async with _client(_app()) as c:
        r = await c.post("/v1/m/infer", content=b"x", headers={"X-API-Key": APIKEY})
        assert r.status_code != 401  # enqueued -> 202/200, not auth-rejected


async def test_healthz_open_even_with_key():
    async with _client(_app()) as c:
        assert (await c.get("/healthz")).status_code == 200


async def test_internal_broker_exempt_uses_jwt_not_api_key():
    # the worker broker must keep working off its JWT alone — the api-key gate must NOT block /internal.
    tok = mint_worker_token(app="m", worker_id="w1", key=KEY)
    async with _client(_app()) as c:
        r = await c.post("/internal/v1/lease", json={"max": 1}, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code != 401  # JWT accepted; api-key gate did not interfere


async def test_no_key_configured_is_open():
    async with _client(_app(api_key=None)) as c:
        assert (await c.post("/v1/m/infer", content=b"x")).status_code != 401
