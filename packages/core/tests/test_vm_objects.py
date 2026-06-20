import json

import pytest
from sluice_core.errors import KeyNotFound
from sluice_core.vm_objects import VmObjects


class _Store:
    def __init__(self):
        self.kv = {}

    async def put(self, key, body, content_type=None):
        self.kv[key] = body

    async def get(self, key):
        if key not in self.kv:
            raise KeyNotFound(key)
        return self.kv[key]

    async def delete(self, key):
        self.kv.pop(key, None)


@pytest.mark.asyncio
async def test_should_write_heartbeat_doc_when_put_heartbeat():
    s = _Store()
    vo = VmObjects(s, root="sluice")
    await vo.put_heartbeat("app1", "vm1", {"phase": "running", "workers": 3})
    assert json.loads(s.kv["sluice/apps/app1/vms/vm1/heartbeat.json"])["phase"] == "running"


@pytest.mark.asyncio
async def test_should_return_action_then_none_when_pop_command():
    s = _Store()
    vo = VmObjects(s, root="sluice")
    s.kv["sluice/apps/app1/vms/vm1/desired.json"] = json.dumps({"action": "shutdown"}).encode()
    assert await vo.pop_command("app1", "vm1") == "shutdown"
    assert await vo.pop_command("app1", "vm1") is None  # popped (deleted)
