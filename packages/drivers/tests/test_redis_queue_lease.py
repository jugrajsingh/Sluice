import asyncio

import pytest
from sluice_drivers.redis_queue import RedisQueue


@pytest.fixture
def client():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis()


async def test_should_be_reclaimable_when_lease_not_extended(client):
    q = RedisQueue(client=client, idle_reclaim_ms=50)
    await q.enqueue("s", b"job")
    first = (await q.receive("s", max_messages=1, wait_seconds=0))[0]
    await asyncio.sleep(0.08)  # exceed the 50ms window without extending
    # a second consumer reclaims it
    q2 = RedisQueue(client=client, consumer="c2", idle_reclaim_ms=50)
    again = await q2.receive("s", max_messages=1, wait_seconds=0)
    assert again and again[0].ack_token == first.ack_token  # redelivered


async def test_should_retain_ownership_when_heartbeated_within_window(client):
    q = RedisQueue(client=client, idle_reclaim_ms=50)
    await q.enqueue("s", b"job")
    msg = (await q.receive("s", max_messages=1, wait_seconds=0))[0]
    for _ in range(4):  # heartbeat inside the window
        await asyncio.sleep(0.03)
        await q.extend_lease("s", msg, seconds=1)
    q2 = RedisQueue(client=client, consumer="c2", idle_reclaim_ms=50)
    assert await q2.receive("s", max_messages=1, wait_seconds=0) == []  # still owned, not reclaimable
