from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque

from ..errors import UnknownAckToken
from ..models import Message, QueueDepth


class MemoryQueue:
    """In-process reference Queue with a visibility-timeout lease model."""

    def __init__(self, *, default_lease_s: int = 30) -> None:
        self._ready: dict[str, deque[Message]] = defaultdict(deque)
        self._inflight: dict[str, dict[str, tuple[Message, float]]] = defaultdict(dict)
        self._default_lease_s = default_lease_s
        self._lock = asyncio.Lock()

    def _requeue_expired(self, source: str) -> None:
        now = time.monotonic()
        expired = [tok for tok, (_m, deadline) in self._inflight[source].items() if deadline <= now]
        for tok in expired:
            msg, _ = self._inflight[source].pop(tok)
            requeued = msg.model_copy(update={"ack_token": "", "receive_count": msg.receive_count + 1})
            self._ready[source].appendleft(requeued)

    async def enqueue(self, dest: str, body: bytes, *, attributes: dict[str, str] | None = None) -> str:
        mid = uuid.uuid4().hex
        async with self._lock:
            self._ready[dest].append(Message(id=mid, body=body, attributes=attributes or {}))
        return mid

    async def receive(self, source: str, *, max_messages: int, wait_seconds: int) -> list[Message]:
        deadline = time.monotonic() + wait_seconds
        while True:
            async with self._lock:
                self._requeue_expired(source)
                out: list[Message] = []
                while self._ready[source] and len(out) < max_messages:
                    msg = self._ready[source].popleft()
                    token = uuid.uuid4().hex
                    leased = msg.model_copy(update={"ack_token": token})
                    self._inflight[source][token] = (leased, time.monotonic() + self._default_lease_s)
                    out.append(leased)
                if out:
                    return out
            if time.monotonic() >= deadline:
                return []
            await asyncio.sleep(0.01)

    async def ack(self, source: str, msg: Message) -> None:
        async with self._lock:
            if self._inflight[source].pop(msg.ack_token, None) is None:
                raise UnknownAckToken(msg.ack_token)

    async def nack(self, source: str, msg: Message) -> None:
        async with self._lock:
            entry = self._inflight[source].pop(msg.ack_token, None)
            if entry is None:
                raise UnknownAckToken(msg.ack_token)
            self._ready[source].appendleft(entry[0].model_copy(update={"ack_token": ""}))

    async def extend_lease(self, source: str, msg: Message, seconds: int) -> None:
        async with self._lock:
            entry = self._inflight[source].get(msg.ack_token)
            if entry is None:
                raise UnknownAckToken(msg.ack_token)
            self._inflight[source][msg.ack_token] = (entry[0], time.monotonic() + seconds)

    async def depth(self, source: str) -> QueueDepth:
        async with self._lock:
            self._requeue_expired(source)
            return QueueDepth(visible=len(self._ready[source]), in_flight=len(self._inflight[source]))
