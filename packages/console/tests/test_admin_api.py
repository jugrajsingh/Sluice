import httpx
from sluice_console.app import build_console_app
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.models import QueueDepth
from sluice_core.testing.fakes import FakeObjectStore

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


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_apply_then_get_then_delete():
    reg = ObjectStoreAppRegistry(store=FakeObjectStore())
    app = build_console_app(registry=reg, queue=_Q(), inspector=_I())
    async with _client(app) as c:
        r = await c.put("/v1/apps/topwear", content=APP_YAML)
        assert r.status_code == 200
        assert (await reg.get_app("topwear")).image == "repo/x:1"
        listed = await c.get("/v1/apps")
        assert listed.json()[0]["name"] == "topwear"
        d = await c.delete("/v1/apps/topwear")
        assert d.status_code == 200
        assert await reg.get_app("topwear") is None


async def test_apply_rejects_invalid_and_name_mismatch():
    app = build_console_app(registry=ObjectStoreAppRegistry(store=FakeObjectStore()), queue=_Q(), inspector=_I())
    async with _client(app) as c:
        bad = await c.put("/v1/apps/topwear", content="apiVersion: nope")
        assert bad.status_code == 422
        mismatch = await c.put("/v1/apps/other", content=APP_YAML)
        assert mismatch.status_code == 422
