"""Reusable conformance tests. Subclass and provide `queue` and `source` fixtures."""

from __future__ import annotations

import pytest

from sluice_core.interfaces import Queue


class QueueConformance:
    @pytest.fixture
    def queue(self) -> Queue:  # pragma: no cover - overridden
        raise NotImplementedError

    @pytest.fixture
    def source(self) -> str:
        return "conformance-source"

    async def test_satisfies_protocol(self, queue: Queue) -> None:
        assert isinstance(queue, Queue)

    async def test_enqueue_then_receive_roundtrip(self, queue: Queue, source: str) -> None:
        await queue.enqueue(source, b"hello")
        msgs = await queue.receive(source, max_messages=1, wait_seconds=2)
        assert len(msgs) == 1
        assert msgs[0].body == b"hello"
        assert msgs[0].ack_token != ""

    async def test_receive_empty_returns_empty_list(self, queue: Queue, source: str) -> None:
        assert await queue.receive(source, max_messages=5, wait_seconds=0) == []

    async def test_ack_prevents_redelivery(self, queue: Queue, source: str) -> None:
        await queue.enqueue(source, b"x")
        msgs = await queue.receive(source, max_messages=1, wait_seconds=2)
        await queue.ack(source, msgs[0])
        assert await queue.receive(source, max_messages=1, wait_seconds=0) == []

    async def test_nack_returns_message(self, queue: Queue, source: str) -> None:
        await queue.enqueue(source, b"x")
        m = (await queue.receive(source, max_messages=1, wait_seconds=2))[0]
        await queue.nack(source, m)
        again = await queue.receive(source, max_messages=1, wait_seconds=2)
        assert len(again) == 1

    async def test_depth_counts_visible(self, queue: Queue, source: str) -> None:
        await queue.enqueue(source, b"a")
        await queue.enqueue(source, b"b")
        d = await queue.depth(source)
        assert d.visible == 2

    async def test_extend_lease_keeps_message_in_flight(self, queue: Queue, source: str) -> None:
        await queue.enqueue(source, b"x")
        m = (await queue.receive(source, max_messages=1, wait_seconds=2))[0]
        await queue.extend_lease(source, m, 60)
        d = await queue.depth(source)
        assert d.in_flight == 1
        assert d.visible == 0
