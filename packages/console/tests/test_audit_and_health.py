import logging

import httpx
from sluice_console.app import build_console_app
from sluice_core.models import AppSpec, QueueDepth, ScalingSpec


def _app():
    return AppSpec(name="m", image="i", handler="h:H", scaling=ScalingSpec())


class _Reg:
    async def list_apps(self):
        return [_app()]

    async def get_app(self, name):
        return _app()

    async def put_app(self, spec): ...
    async def delete_app(self, name): ...
    async def set_desired_state(self, name, state): ...
    async def write_status(self, name, status): ...
    async def get_status(self, name):
        return None


class _Q:
    async def depth(self, s):
        return QueueDepth()


class _I:
    async def workers(self, a):
        return []


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_healthz():
    app = build_console_app(registry=_Reg(), queue=_Q(), inspector=_I())
    async with _client(app) as c:
        assert (await c.get("/healthz")).status_code == 200


async def test_pause_is_audit_logged(caplog):
    app = build_console_app(registry=_Reg(), queue=_Q(), inspector=_I())
    with caplog.at_level(logging.INFO, logger="sluice.audit"):
        async with _client(app) as c:
            await c.post("/v1/apps/m/pause")
    assert any("action=pause app=m" in r.getMessage() for r in caplog.records)
