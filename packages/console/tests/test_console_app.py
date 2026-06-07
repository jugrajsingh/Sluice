import httpx
from sluice_console.app import build_console_app
from sluice_core.models import AppSpec, AppStatus, QueueDepth, ScalingSpec, WorkerState, WorkerStatus


def _app():
    return AppSpec(name="topwear", image="i", handler="h:H", scaling=ScalingSpec(schedule_grace_s=180))


class FakeRegistry:
    def __init__(self, apps):
        self._apps = apps
        self.state_calls = []

    async def list_apps(self):
        return self._apps

    async def get_app(self, name):
        return next((a for a in self._apps if a.name == name), None)

    async def put_app(self, spec): ...

    async def delete_app(self, name): ...

    async def set_desired_state(self, name, state):
        self.state_calls.append((name, state))

    async def write_status(self, name, status: AppStatus): ...

    async def get_status(self, name):
        return None


class FakeQueue:
    async def depth(self, source):
        return QueueDepth(visible=412, in_flight=8)


class FakeInspector:
    def __init__(self, workers):
        self._w = workers

    async def workers(self, app):
        return self._w


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_list_apps_aggregates_with_held_status():
    stuck = [WorkerStatus(pod="p", state=WorkerState.unschedulable, age_s=999, reason="ZONE_RESOURCE_POOL_EXHAUSTED")]
    reg = FakeRegistry([_app()])
    app = build_console_app(registry=reg, queue=FakeQueue(), inspector=FakeInspector(stuck))
    async with _client(app) as c:
        r = await c.get("/v1/apps")
    body = r.json()
    assert r.status_code == 200 and body[0]["scale_status"] == "held"
    assert body[0]["queue"]["visible"] == 412
    assert body[0]["workers"]["unschedulable"] == 1


async def test_pause_patches_spec_store():
    reg = FakeRegistry([_app()])
    app = build_console_app(registry=reg, queue=FakeQueue(), inspector=FakeInspector([]))
    async with _client(app) as c:
        r = await c.post("/v1/apps/topwear/pause")
    assert r.status_code == 200
    assert ("topwear", "Paused") in reg.state_calls


async def test_resume_patches_spec_store():
    reg = FakeRegistry([_app()])
    app = build_console_app(registry=reg, queue=FakeQueue(), inspector=FakeInspector([]))
    async with _client(app) as c:
        await c.post("/v1/apps/topwear/resume")
    assert ("topwear", "Ready") in reg.state_calls
