import httpx
from sluice_console.app import build_console_app
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.models import QueueDepth
from sluice_core.testing.fakes import FakeObjectStore

APIKEY = "test-api-key"  # gitleaks:allow (test fixture, not a secret)
APP_YAML = """
apiVersion: sluice/v1
kind: App
metadata: { name: topwear }
spec: { image: repo/x:1, handler: "h:H" }
"""


class _Q:
    async def depth(self, s):
        return QueueDepth()


class _I:
    async def workers(self, a):
        return []


def _app(api_key=APIKEY):
    return build_console_app(
        registry=ObjectStoreAppRegistry(store=FakeObjectStore()), queue=_Q(), inspector=_I(), api_key=api_key
    )


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_app_management_requires_key():
    async with _client(_app()) as c:
        assert (await c.get("/v1/apps")).status_code == 401
        assert (await c.put("/v1/apps/topwear", content=APP_YAML)).status_code == 401
        bad = await c.put("/v1/apps/topwear", content=APP_YAML, headers={"X-API-Key": "nope"})
        assert bad.status_code == 401


async def test_app_management_accepts_correct_key():
    async with _client(_app()) as c:
        r = await c.put("/v1/apps/topwear", content=APP_YAML, headers={"X-API-Key": APIKEY})
        assert r.status_code == 200


async def test_healthz_open_even_with_key():
    async with _client(_app()) as c:
        assert (await c.get("/healthz")).status_code == 200


async def test_no_key_configured_is_open():
    async with _client(_app(api_key=None)) as c:
        assert (await c.get("/v1/apps")).status_code == 200
