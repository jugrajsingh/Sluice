import httpx
from sluice_console.app import build_console_app
from sluice_console.static import mount_web


class _Reg:
    async def list_apps(self):
        return []

    async def get_app(self, name):
        return None

    async def put_app(self, spec): ...
    async def delete_app(self, name): ...
    async def set_desired_state(self, name, state): ...
    async def write_status(self, name, status): ...
    async def get_status(self, name):
        return None


class _Q:
    async def depth(self, s):
        from sluice_core.models import QueueDepth

        return QueueDepth()


class _I:
    async def workers(self, a):
        return []


async def test_index_served(tmp_path):
    (tmp_path / "index.html").write_text("<html>sluice</html>")
    app = build_console_app(registry=_Reg(), queue=_Q(), inspector=_I())
    mount_web(app, str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/")
    assert r.status_code == 200 and "sluice" in r.text
