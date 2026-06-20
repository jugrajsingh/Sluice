import httpx
from sluice_console.app import build_console_app
from sluice_core.app_yaml import parse_app_yaml
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.interfaces import NoWorkerPods
from sluice_core.models import QueueDepth
from sluice_core.testing.fakes import FakeObjectStore

APP_YAML = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: topwear}\nspec: {image: repo/x:1}\n"


class _Q:
    async def depth(self, s):
        return QueueDepth()


class _Insp:
    def __init__(self, chunks=None, raise_no_pods=False):
        self._chunks = chunks or []
        self._raise = raise_no_pods
        self.seen: dict = {}

    async def workers(self, a):
        return []

    async def pod_logs(self, app, *, pod=None, since_seconds=None, tail=200, follow=False):
        self.seen = {"pod": pod, "since_seconds": since_seconds, "tail": tail, "follow": follow}
        if self._raise:
            raise NoWorkerPods("no worker pods for app")
        for c in self._chunks:
            yield c


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _seeded_registry() -> ObjectStoreAppRegistry:
    reg = ObjectStoreAppRegistry(store=FakeObjectStore())
    await reg.put_app(parse_app_yaml(APP_YAML))
    return reg


async def test_should_stream_pod_logs_and_forward_query_params():
    insp = _Insp(chunks=[b"line1\n", b"line2\n"])
    app = build_console_app(registry=await _seeded_registry(), queue=_Q(), inspector=insp)
    async with _client(app) as c:
        r = await c.get("/v1/apps/topwear/logs", params={"worker": "p1", "since": 60, "tail": 10, "follow": True})
        assert r.status_code == 200 and r.content == b"line1\nline2\n"
    assert insp.seen == {"pod": "p1", "since_seconds": 60, "tail": 10, "follow": True}


async def test_should_404_when_app_unknown():
    app = build_console_app(registry=ObjectStoreAppRegistry(store=FakeObjectStore()), queue=_Q(), inspector=_Insp())
    async with _client(app) as c:
        r = await c.get("/v1/apps/ghost/logs")
        assert r.status_code == 404


async def test_should_400_when_no_worker_pods():
    insp = _Insp(raise_no_pods=True)
    app = build_console_app(registry=await _seeded_registry(), queue=_Q(), inspector=insp)
    async with _client(app) as c:
        r = await c.get("/v1/apps/topwear/logs")
        assert r.status_code == 400 and "no worker pods" in r.text
