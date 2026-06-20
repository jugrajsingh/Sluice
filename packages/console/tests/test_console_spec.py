import httpx
from sluice_console.app import build_console_app
from sluice_core.app_yaml import parse_app_yaml
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.models import QueueDepth
from sluice_core.testing.fakes import FakeObjectStore

APP_YAML = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: topwear}\nspec: {image: repo/x:1}\n"


class _Q:
    async def depth(self, s):
        return QueueDepth()


class _I:
    async def workers(self, a):
        return []


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_should_return_serialized_spec_that_round_trips():
    reg = ObjectStoreAppRegistry(store=FakeObjectStore())
    await reg.put_app(parse_app_yaml(APP_YAML))
    app = build_console_app(registry=reg, queue=_Q(), inspector=_I())
    async with _client(app) as c:
        r = await c.get("/v1/apps/topwear/spec")
        assert r.status_code == 200
        spec = parse_app_yaml(r.text)  # the body is a re-appliable App spec
        assert spec.name == "topwear" and spec.image == "repo/x:1"


async def test_should_404_spec_for_unknown_app():
    app = build_console_app(registry=ObjectStoreAppRegistry(store=FakeObjectStore()), queue=_Q(), inspector=_I())
    async with _client(app) as c:
        r = await c.get("/v1/apps/ghost/spec")
        assert r.status_code == 404


async def test_should_report_version_without_auth():
    # /healthz/version is exempt — usable as an unauthenticated connectivity check.
    app = build_console_app(
        registry=ObjectStoreAppRegistry(store=FakeObjectStore()), queue=_Q(), inspector=_I(), api_key="secret"
    )
    async with _client(app) as c:
        r = await c.get("/healthz/version")  # no X-API-Key sent
        assert r.status_code == 200 and isinstance(r.json().get("version"), str)
