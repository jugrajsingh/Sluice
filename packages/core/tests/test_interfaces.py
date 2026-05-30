from sluice_core.interfaces import ObjectStore, Queue
from sluice_core.models import QueueDepth


class _StubQueue:
    async def enqueue(self, dest, body, *, attributes=None):
        return "id"

    async def receive(self, source, *, max_messages, wait_seconds):
        return []

    async def ack(self, source, msg):
        return None

    async def nack(self, source, msg):
        return None

    async def extend_lease(self, source, msg, seconds):
        return None

    async def depth(self, source):
        return QueueDepth()


class _StubStore:
    async def put(self, key, data, *, content_type=None):
        return None

    async def get(self, key):
        return b""

    async def exists(self, key):
        return False

    async def delete(self, key):
        return None

    async def signed_url(self, key, *, expires_s):
        return "url"

    async def list_keys(self, prefix):
        return []


def test_stub_satisfies_protocols():
    assert isinstance(_StubQueue(), Queue)
    assert isinstance(_StubStore(), ObjectStore)
