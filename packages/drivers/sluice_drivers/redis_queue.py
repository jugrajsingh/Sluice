from __future__ import annotations

from sluice_core.errors import UnknownAckToken
from sluice_core.models import Message, QueueDepth


class RedisQueue:
    """Queue over Redis Streams. ack_token = stream entry id.

    Lease ownership is idle-time based: a message that has not been acknowledged or
    heartbeated within ``idle_reclaim_ms`` milliseconds becomes reclaimable by any
    consumer that calls ``receive``.

    For short-lived inference jobs the default window (30 s) is sufficient.  For
    bulk-batch processing — where a single file can take minutes — construct the
    queue with a long window (e.g. ``idle_reclaim_ms=900_000`` for 15 minutes) and
    call ``extend_lease`` periodically well inside that window to retain ownership.
    """

    def __init__(self, *, client, group: str = "sluice", consumer: str = "c1", idle_reclaim_ms: int = 30_000) -> None:
        self._r = client
        self._group = group
        self._consumer = consumer
        self._idle = idle_reclaim_ms

    async def _ensure_group(self, source: str) -> None:
        try:
            await self._r.xgroup_create(name=source, groupname=self._group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP if it already exists
            if "BUSYGROUP" not in str(e):
                raise

    async def enqueue(self, dest: str, body: bytes, *, attributes: dict[str, str] | None = None) -> str:
        fields = {"body": body, **{f"a:{k}": v for k, v in (attributes or {}).items()}}
        eid = await self._r.xadd(dest, fields)
        return eid.decode() if isinstance(eid, bytes) else str(eid)

    async def receive(self, source: str, *, max_messages: int, wait_seconds: int) -> list[Message]:
        await self._ensure_group(source)
        # 1) reclaim idle (redelivery) then 2) read new
        out: list[Message] = []
        claimed = await self._r.xautoclaim(
            source, self._group, self._consumer, min_idle_time=self._idle, start_id="0-0", count=max_messages
        )
        out += [self._to_msg(eid, fields) for eid, fields in (claimed[1] or [])]
        if len(out) < max_messages:
            resp = await self._r.xreadgroup(
                self._group,
                self._consumer,
                {source: ">"},
                count=max_messages - len(out),
                block=wait_seconds * 1000 or None,
            )
            for _stream, entries in resp or []:
                out += [self._to_msg(eid, fields) for eid, fields in entries]
        return out

    def _to_msg(self, eid, fields) -> Message:
        d = {(k.decode() if isinstance(k, bytes) else k): v for k, v in fields.items()}
        body = d.get("body", b"")
        attrs = {k[2:]: (v.decode() if isinstance(v, bytes) else v) for k, v in d.items() if k.startswith("a:")}
        tok = eid.decode() if isinstance(eid, bytes) else str(eid)
        return Message(
            id=tok, body=body if isinstance(body, bytes) else str(body).encode(), attributes=attrs, ack_token=tok
        )

    async def ack(self, source: str, msg: Message) -> None:
        n = await self._r.xack(source, self._group, msg.ack_token)
        if not n:
            raise UnknownAckToken(msg.ack_token)
        await self._r.xdel(source, msg.ack_token)

    async def nack(self, source: str, msg: Message) -> None:
        # leave pending → redelivered after idle_reclaim; nothing to do
        return None

    async def extend_lease(self, source: str, msg: Message, seconds: int) -> None:
        """Reset the idle timer for ``msg``, retaining ownership for another ``idle_reclaim_ms`` window.

        Call this within ``idle_reclaim_ms`` milliseconds of the last ``receive`` or
        ``extend_lease`` call to prevent the message from being reclaimed by another
        consumer.  ``seconds`` documents the caller's intended heartbeat budget (how
        long the caller expects to hold the lease before the next heartbeat) but does
        not directly control the reclaim window — that is set by ``idle_reclaim_ms``
        on the queue constructor.  Batch processors should pass a value comfortably
        below ``idle_reclaim_ms / 1000`` so clock skew cannot cause an accidental
        reclaim between heartbeats.
        """
        # idle-based model: re-claim with min_idle_time=0 to reset idle timer
        await self._r.xclaim(source, self._group, self._consumer, min_idle_time=0, message_ids=[msg.ack_token])

    async def depth(self, source: str) -> QueueDepth:
        await self._ensure_group(source)
        total = await self._r.xlen(source)
        pending = await self._r.xpending(source, self._group)
        in_flight = pending["pending"] if isinstance(pending, dict) else (pending[0] if pending else 0)
        return QueueDepth(visible=max(total - in_flight, 0), in_flight=in_flight)
