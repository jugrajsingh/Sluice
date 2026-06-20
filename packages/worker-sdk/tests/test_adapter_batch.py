"""Behavioral tests for the dual-source adapter.

The adapter feeds ONE local model server from two lanes — infer (low-latency,
priority) and batch (bulk JSONL, backfill) — under a SINGLE shared
``asyncio.Semaphore(put_concurrency)``. These tests prove the hard invariants:

1. A whole batch file is processed and acked exactly once when infer is idle.
2. The shared semaphore is never oversubscribed: total concurrent in-flight
   calls (infer ``server`` + batch ``call_record``) never exceed
   ``put_concurrency`` even when both lanes contend.
3. Infer is preferred over batch: infer items complete before batch backfill.
4. A poison file (redelivered past the max) is marked failed and acked without
   being processed.

Fakes mirror ``test_adapter.py``'s ``FakeBroker`` shape.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from sluice_core.batch_models import BatchFileStatus
from sluice_core.models import Message
from sluice_worker.adapter import MAX_FILE_REDELIVERIES, Adapter

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBroker:
    """Infer broker: leases dict items in chunks across calls (like the real broker).

    ``lease(n)`` returns up to ``n`` of the remaining pooled items per call, so a
    pool wider than the free-slot budget is drained over multiple leases.
    """

    def __init__(self, items: list[dict], bodies: dict[str, bytes]) -> None:
        self._pool = list(items)
        self.bodies = dict(bodies)
        self.puts: dict[str, bytes] = {}
        self.acked: list[str] = []
        self.nacked: list[str] = []
        self.extended: list[str] = []

    async def lease(self, n: int) -> list[dict]:
        if n <= 0:
            return []
        out = [self._pool.pop(0) for _ in range(min(n, len(self._pool)))]
        return out

    async def extend(self, lease_ids: list[str]) -> None:
        self.extended.extend(lease_ids)

    async def get(self, url: str) -> bytes:
        return self.bodies[url]

    async def put(self, url: str, data: bytes) -> None:
        self.puts[url] = data

    async def ack(self, lease_id: str) -> None:
        self.acked.append(lease_id)

    async def nack(self, lease_id: str) -> None:
        self.nacked.append(lease_id)


class FakeBatchBroker:
    """Batch broker: leases Message objects one at a time, then drains empty."""

    def __init__(self, messages: list[Message], bodies: dict[str, bytes]) -> None:
        self._messages = list(messages)
        self._idx = 0
        self.bodies = dict(bodies)
        self.acked: list[str] = []
        self.nacked: list[str] = []
        self.extended: list[str] = []

    async def lease(self, n: int) -> list[Message]:
        if self._idx >= len(self._messages):
            return []
        msg = self._messages[self._idx]
        self._idx += 1
        return [msg]

    async def extend(self, lease_ids: list[str]) -> None:
        self.extended.extend(lease_ids)

    async def get(self, url: str) -> bytes:
        return self.bodies[url]

    async def put(self, url: str, data: bytes) -> None:
        pass

    async def ack(self, lease_id: str) -> None:
        self.acked.append(lease_id)

    async def nack(self, lease_id: str) -> None:
        self.nacked.append(lease_id)


class FakeBatchObjects:
    """Duck-types BatchObjects: put_output_part, put_file_status, get_file_status."""

    def __init__(self, existing_status: BatchFileStatus | None = None) -> None:
        self._existing = existing_status
        self.written: list[BatchFileStatus] = []
        self.parts: dict[int, bytes] = {}

    async def get_file_status(self, app: str, job_id: str, filename: str) -> BatchFileStatus | None:
        return self._existing

    async def put_file_status(self, app: str, job_id: str, status: BatchFileStatus) -> None:
        self.written.append(status)

    async def put_output_part(self, app: str, job_id: str, filename: str, start_offset: int, body: bytes) -> None:
        self.parts[start_offset] = body


def _make_server(handler: Any = None) -> httpx.AsyncClient:
    if handler is None:

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"ok")

    return httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handler))


def _adapter(broker: Any, server: httpx.AsyncClient, **kw: Any) -> Adapter:
    kw.setdefault("concurrency", 1)
    kw.setdefault("max_blank_retries", 1)
    return Adapter(
        broker=broker,
        server=server,
        request_path="/v1/infer",
        method="POST",
        content_type="application/json",
        **kw,
    )


def _batch_msg(*, mid: str, job_id: str, file: str, body_url: str, receive_count: int = 1) -> Message:
    return Message(
        id=mid,
        body=b"",
        attributes={"job_id": job_id, "file": file, "body_url": body_url},
        ack_token=f"tok-{mid}",
        receive_count=receive_count,
    )


# ---------------------------------------------------------------------------
# Test 1: whole batch file processed and acked exactly once when infer idle
# ---------------------------------------------------------------------------


async def test_should_process_whole_batch_file_and_ack_once_when_infer_idle() -> None:
    """Empty infer queue + one batch file of 4 records -> all 4 processed, file acked once."""
    lines = [json.dumps({"_rid": f"r{i}", "x": i}).encode() for i in range(4)]
    body = b"\n".join(lines)

    batch_broker = FakeBatchBroker(
        [_batch_msg(mid="bm1", job_id="J1", file="data.jsonl", body_url="https://s3/batch-body")],
        {"https://s3/batch-body": body},
    )
    batch_objects = FakeBatchObjects()
    infer_broker = FakeBroker([], {})

    processed_records: list[bytes] = []

    async def fake_call_record(record: bytes) -> bytes:
        processed_records.append(record)
        return b'{"ok": true}'

    adapter = _adapter(
        infer_broker,
        _make_server(),
        app="myapp",
        batch_source=batch_broker.lease,
        batch_broker=batch_broker,
        batch_objects=batch_objects,
        batch_call_record=fake_call_record,
        batch_output_partition_size=10,
        infer_presence_poll_s=0.01,
        batch_heartbeat_s=0.01,
    )
    await asyncio.wait_for(adapter.run(), timeout=5.0)

    assert len(processed_records) == 4, f"expected 4 records processed, got {len(processed_records)}"
    assert batch_broker.acked == ["tok-bm1"], f"file should be acked exactly once: {batch_broker.acked}"
    assert batch_broker.nacked == [], "file should not be nacked"
    assert batch_objects.written, "put_file_status should have been called"
    final_status = batch_objects.written[-1]
    assert final_status.state == "completed"
    assert final_status.records_done == 4


# ---------------------------------------------------------------------------
# Test 2: THE KEY TEST — shared semaphore is never oversubscribed under contention
# ---------------------------------------------------------------------------


async def test_should_never_exceed_put_concurrency_when_infer_and_batch_contend() -> None:
    """put_concurrency=4: many infer items AND a large batch file contend.

    BOTH lanes increment a shared in-flight counter on entry, hold the slot across
    a real ``await asyncio.sleep`` (so overlap genuinely occurs), then decrement on
    exit. The starvation floor is enabled (short grace) so batch is guaranteed to
    run concurrently with infer rather than only after infer drains.

    Two assertions: (1) the MAX observed concurrent in-flight count is <=
    put_concurrency (proves the ONE shared semaphore — the headline invariant);
    (2) the MAX is >= 2 (proves real overlap was observed, so (1) is not vacuous).

    On the old two-independent-loops code this FAILS: infer (its own concurrency=4)
    and batch (its own budget / per-record fan-out) overlap above 4 because nothing
    couples their counts to a single semaphore.
    """
    put_concurrency = 4
    hold_s = 0.02

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _bump() -> None:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        # Hold the shared slot across a real await so concurrent occupancy is observable.
        await asyncio.sleep(hold_s)

    async def _enter() -> None:
        await _bump()

    async def _exit() -> None:
        nonlocal in_flight
        async with lock:
            in_flight -= 1

    # 12 infer items.
    infer_items = [
        {"lease_id": f"L{i}", "body_url": f"https://s3/ib{i}", "result_url": f"https://s3/ir{i}"} for i in range(12)
    ]
    infer_bodies = {f"https://s3/ib{i}": b'{"in": %d}' % i for i in range(12)}
    infer_broker = FakeBroker(infer_items, infer_bodies)

    # One batch file with 12 records.
    batch_lines = [json.dumps({"_rid": f"br{i}"}).encode() for i in range(12)]
    batch_broker = FakeBatchBroker(
        [_batch_msg(mid="bmK", job_id="JK", file="big.jsonl", body_url="https://s3/big")],
        {"https://s3/big": b"\n".join(batch_lines)},
    )
    batch_objects = FakeBatchObjects()

    def server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"r": 1}')

    async def fake_call_record(record: bytes) -> bytes:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(hold_s)
            return b'{"r": 2}'
        finally:
            async with lock:
                in_flight -= 1

    adapter = _adapter(
        infer_broker,
        _make_server(server_handler),
        concurrency=put_concurrency,  # adapter's own infer concurrency knob
        app="myapp",
        batch_source=batch_broker.lease,
        batch_broker=batch_broker,
        batch_objects=batch_objects,
        batch_call_record=fake_call_record,
        batch_output_partition_size=100,
        put_concurrency=put_concurrency,
        # Short starve grace -> the floor reserves a batch slot so batch overlaps
        # with infer instead of waiting for infer to fully drain.
        starve_grace_s=0.0,
        infer_presence_poll_s=0.002,
        batch_heartbeat_s=0.01,
        infer_inflight_hooks=(_enter, _exit),
    )
    await asyncio.wait_for(adapter.run(), timeout=10.0)

    assert max_in_flight <= put_concurrency, (
        f"oversubscription: max concurrent in-flight {max_in_flight} > put_concurrency {put_concurrency}"
    )
    assert max_in_flight >= 2, f"test vacuous: never observed overlap (max_in_flight={max_in_flight})"
    # And all work completed.
    assert len(infer_broker.acked) == 12, f"all infer items acked: {len(infer_broker.acked)}"
    assert batch_broker.acked == ["tok-bmK"], f"batch file acked: {batch_broker.acked}"


# ---------------------------------------------------------------------------
# Test 3: infer preferred over batch when infer items are present
# ---------------------------------------------------------------------------


async def test_should_prefer_infer_over_batch_when_infer_present() -> None:
    """Infer items present alongside a batch file -> infer dispatched/completed before batch backfill.

    Record dispatch order: every infer dispatch must appear before the first batch
    record dispatch (full-yield to infer).
    """
    infer_items = [
        {"lease_id": f"L{i}", "body_url": f"https://s3/ib{i}", "result_url": f"https://s3/ir{i}"} for i in range(4)
    ]
    infer_bodies = {f"https://s3/ib{i}": b"{}" for i in range(4)}
    infer_broker = FakeBroker(infer_items, infer_bodies)

    batch_lines = [json.dumps({"_rid": f"br{i}"}).encode() for i in range(4)]
    batch_broker = FakeBatchBroker(
        [_batch_msg(mid="bm2", job_id="J2", file="b.jsonl", body_url="https://s3/batch2")],
        {"https://s3/batch2": b"\n".join(batch_lines)},
    )
    batch_objects = FakeBatchObjects()

    dispatch_order: list[str] = []
    order_lock = asyncio.Lock()

    def server_handler(request: httpx.Request) -> httpx.Response:
        dispatch_order.append("infer")
        return httpx.Response(200, content=b'{"r": 1}')

    async def fake_call_record(record: bytes) -> bytes:
        async with order_lock:
            dispatch_order.append("batch")
        await asyncio.sleep(0)
        return b'{"r": 2}'

    adapter = _adapter(
        infer_broker,
        _make_server(server_handler),
        concurrency=2,
        app="myapp",
        batch_source=batch_broker.lease,
        batch_broker=batch_broker,
        batch_objects=batch_objects,
        batch_call_record=fake_call_record,
        batch_output_partition_size=10,
        put_concurrency=4,
        # Long starve grace so the floor never fires during the test -> pure full-yield.
        starve_grace_s=3600.0,
        infer_presence_poll_s=0.005,
        batch_heartbeat_s=0.01,
    )
    await asyncio.wait_for(adapter.run(), timeout=5.0)

    assert dispatch_order.count("infer") == 4, f"all infer dispatched: {dispatch_order}"
    assert dispatch_order.count("batch") == 4, f"all batch records dispatched: {dispatch_order}"
    # Full-yield: every infer dispatch precedes the first batch record dispatch.
    first_batch = dispatch_order.index("batch")
    assert all(x == "infer" for x in dispatch_order[:first_batch]), (
        f"infer must be served before batch resumes: {dispatch_order}"
    )
    assert dispatch_order[:4] == ["infer"] * 4, f"all 4 infer should precede batch: {dispatch_order}"
    assert infer_broker.acked == [f"L{i}" for i in range(4)] or sorted(infer_broker.acked) == [
        f"L{i}" for i in range(4)
    ], f"infer items acked: {infer_broker.acked}"
    assert batch_broker.acked == ["tok-bm2"], f"batch file acked: {batch_broker.acked}"


# ---------------------------------------------------------------------------
# Test 4: poison-file guard — redelivered past max -> failed + acked, not processed
# ---------------------------------------------------------------------------


async def test_should_mark_file_failed_when_redelivered_past_max() -> None:
    """receive_count > MAX_FILE_REDELIVERIES -> file marked failed and acked, never processed."""
    body = b"\n".join([json.dumps({"_rid": "r0"}).encode()])
    batch_broker = FakeBatchBroker(
        [
            _batch_msg(
                mid="poison",
                job_id="J3",
                file="poison.jsonl",
                body_url="https://s3/poison",
                receive_count=MAX_FILE_REDELIVERIES + 1,
            )
        ],
        {"https://s3/poison": body},
    )
    batch_objects = FakeBatchObjects()
    infer_broker = FakeBroker([], {})

    processed_records: list[bytes] = []

    async def fake_call_record(record: bytes) -> bytes:
        processed_records.append(record)
        return b"{}"

    adapter = _adapter(
        infer_broker,
        _make_server(),
        app="myapp",
        batch_source=batch_broker.lease,
        batch_broker=batch_broker,
        batch_objects=batch_objects,
        batch_call_record=fake_call_record,
        batch_output_partition_size=10,
        infer_presence_poll_s=0.01,
        batch_heartbeat_s=0.01,
    )
    await asyncio.wait_for(adapter.run(), timeout=5.0)

    assert processed_records == [], f"poison records must not be processed: {processed_records}"
    assert batch_broker.acked == ["tok-poison"], f"poison file should be acked: {batch_broker.acked}"
    assert batch_broker.nacked == [], "poison file should not be nacked"
    assert batch_objects.written, "put_file_status should have been called for poison file"
    failed_status = batch_objects.written[-1]
    assert failed_status.state == "failed", f"expected state='failed', got {failed_status.state}"
    assert failed_status.file == "poison.jsonl"
