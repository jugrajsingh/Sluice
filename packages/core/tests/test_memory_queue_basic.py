import asyncio

from sluice_core.drivers.memory import MemoryQueue


async def test_lease_expiry_redelivers():
    q = MemoryQueue(default_lease_s=0)  # immediate lease expiry
    await q.enqueue("s", b"x")
    first = await q.receive("s", max_messages=1, wait_seconds=0)
    assert len(first) == 1
    await asyncio.sleep(0.01)
    again = await q.receive("s", max_messages=1, wait_seconds=0)  # not acked -> redelivered
    assert len(again) == 1
    assert again[0].receive_count == 2


async def test_ack_removes_message():
    q = MemoryQueue(default_lease_s=30)
    await q.enqueue("s", b"x")
    msgs = await q.receive("s", max_messages=1, wait_seconds=0)
    await q.ack("s", msgs[0])
    d = await q.depth("s")
    assert d.visible == 0 and d.in_flight == 0
