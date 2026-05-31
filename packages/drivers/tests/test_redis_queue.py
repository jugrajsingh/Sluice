import fakeredis.aioredis
import pytest
from sluice_core.testing.queue_conformance import QueueConformance
from sluice_drivers.redis_queue import RedisQueue


class TestRedisQueue(QueueConformance):
    @pytest.fixture
    def queue(self):
        client = fakeredis.aioredis.FakeRedis()
        # idle_reclaim_ms=0 so a nacked (still-pending) message is reclaimable on the
        # very next receive — the conformance nack test re-receives with ~0ms elapsed.
        return RedisQueue(client=client, group="sluice", consumer="c1", idle_reclaim_ms=0)
