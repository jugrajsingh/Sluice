"""In-process test doubles for unit tests across the workspace.

These are NOT deployable backends — they are absent from the driver factory, the
Settings backend lists, and the Helm chart. Deployment requires shared, persistent
backends (Redis/SQS queue; S3/MinIO/GCS object store), which are conformance-tested
against real drivers + emulators (moto/fakeredis) in the `sluice-drivers` package.
Use these only to exercise component logic that needs *some* Queue/ObjectStore.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque

from ..errors import KeyNotFound, UnknownAckToken
from ..models import Message, QueueDepth


class FakeObjectStore:
    """Dict-backed ObjectStore test double. signed_url returns a non-functional placeholder."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self._data[key] = data

    async def get(self, key: str) -> bytes:
        try:
            return self._data[key]
        except KeyError as e:
            raise KeyNotFound(key) from e

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def signed_url(self, key: str, *, method: str = "GET", expires_s: int) -> str:
        return f"memory://{method}/{key}?exp={expires_s}"

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))


class FakeQueue:
    """In-process Queue test double with a visibility-timeout lease model."""

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
