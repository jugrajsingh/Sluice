"""End-to-end batch integration test — the keystone that proves the joints connect.

This is the gap that hid C1/C2: every component had unit tests, but nothing exercised
the REAL wiring across gateway + broker + worker adapter together. This test uses
in-memory fakes ONLY for the object store and queue; everything load-bearing is REAL:

* the REAL ``build_batch_router`` (create / upload-url / submit / status),
* the REAL broker batch routes (``/internal/v1/batch/lease`` etc.),
* the REAL ``BrokerClient`` (its ``batch_lease``/``get``/``batch_ack`` over HTTP),
* the REAL adapter batch lane, constructed the way ``_amain`` constructs it
  (``batch_source`` = ``BrokerClient.batch_lease``, ``batch_broker`` = ``_BatchBrokerView``,
  ``batch_objects`` = ``BatchObjects`` over the same store, ``batch_call_record`` POSTing to
  a fake local model server) — NOT a hand-stubbed ``bodies[body_url]`` map.

Flow proven:
    create job -> upload (seed input in the store) -> submit (enqueues 1 msg) ->
    broker batch-lease yields a presigned body_url -> adapter fetches input via that
    body_url -> processes each record through the local server -> writes output parts +
    status -> the gateway status endpoint reflects ``completed`` and the output parts exist.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi import FastAPI
from sluice_core.batch_models import BatchFileStatus
from sluice_core.batch_objects import BatchObjects
from sluice_core.batch_paths import output_part_key, status_key
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_gateway.app import build_app
from sluice_worker.adapter import Adapter, _BatchBrokerView, _make_batch_call_record
from sluice_worker.batch_writer import BrokerBatchWriter
from sluice_worker.broker_client import BrokerClient

KEY = "e2e-broker-signing-key"  # gitleaks:allow (test fixture, not a secret)
APP = "sam3"


class _PresignedStore(FakeObjectStore):
    """FakeObjectStore whose signed_url returns an http(s) URL the worker's MockTransport
    can resolve back to a key. The real FakeObjectStore returns a non-functional
    ``memory://`` placeholder; here we need the presigned GET to actually fetch bytes so
    the adapter genuinely downloads input via the broker-minted body_url (no stubbing)."""

    async def signed_url(self, key: str, *, method: str = "GET", expires_s: int) -> str:
        return f"http://teststore/{key}"


def _gateway_app(store: FakeObjectStore, queue: FakeQueue) -> FastAPI:
    """Real gateway with the batch router AND the broker batch lane mounted."""
    return build_app(
        queue=queue,
        objects=ObjectStoreInferenceObjects(store=store),
        signing_key=KEY,
        batch_objects=BatchObjects(store=store),
        batch_queue=queue,  # same fake queue instance; broker leases {app}-batch from it
        batch_lease_visibility_s=900,
        t_sync_s=0,
    )


def _worker_transport(gateway_app: FastAPI, store: FakeObjectStore) -> httpx.MockTransport:
    """Route the worker's BrokerClient: control calls -> the real gateway ASGI app;
    presigned object GETs (http://teststore/<key>) -> the shared fake store."""
    asgi = httpx.ASGITransport(app=gateway_app)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "teststore":
            key = request.url.path.lstrip("/")
            if request.method == "PUT":
                # The worker streams an output part to its broker-minted presigned PUT URL.
                store._data[key] = request.content
                return httpx.Response(200)
            data = store._data.get(key)
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data)
        raise AssertionError(f"unexpected non-store URL in mock handler: {request.url}")

    # We cannot mix ASGITransport + MockTransport in one transport, so the BrokerClient
    # gets a transport that dispatches by host: gateway calls go to ASGI, store GETs to the
    # store handler. httpx.MockTransport takes a single handler, so we route inside it.
    async def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "teststore":
            return handler(request)
        # forward to the gateway ASGI app
        resp = await asgi.handle_async_request(request)
        return resp

    return _DispatchTransport(dispatch)


class _DispatchTransport(httpx.AsyncBaseTransport):
    def __init__(self, dispatch) -> None:
        self._dispatch = dispatch

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._dispatch(request)
        # ASGITransport returns a streaming response; read it so content is available.
        await resp.aread()
        return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content)


def _fake_model_server() -> httpx.AsyncClient:
    """Local model server: echoes a result derived from the request body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, content=b"ok")
        record = json.loads(request.content)
        return httpx.Response(200, json={"echo": record.get("_rid", "?"), "x": record.get("x")})

    return httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handler))


async def test_should_complete_batch_job_end_to_end_through_real_wiring() -> None:
    store = _PresignedStore()
    queue = FakeQueue()
    gateway = _gateway_app(store, queue)

    # --- Client side: drive the gateway batch API through its real router. ---
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gw") as gw:
        job_id = (await gw.post(f"/v1/{APP}/batch")).json()["job_id"]

        upload = (await gw.post(f"/v1/{APP}/batch/{job_id}/upload-url", json={"filename": "data.jsonl"})).json()
        assert "url" in upload  # presigned PUT returned

        # Simulate the client uploading the JSONL directly to storage (3 records).
        records = [json.dumps({"_rid": f"r{i}", "x": i}).encode() for i in range(3)]
        await store.put(f"AppData/{APP}/batch/{job_id}/input/data.jsonl", b"\n".join(records))

        submit = await gw.post(f"/v1/{APP}/batch/{job_id}/submit", json={"files": ["data.jsonl"]})
        assert submit.status_code == 200 and submit.json()["submitted"] == 1

    # --- Worker side: construct the batch lane exactly like _amain does. ---
    broker = BrokerClient(
        base_url="http://gw",
        token=_mint(),
        transport=_worker_transport(gateway, store),
    )
    server = _fake_model_server()
    # The worker holds no store creds: output via broker-minted presigned PUT, status via the broker.
    batch_objects = BrokerBatchWriter(broker, app=APP)
    adapter = Adapter(
        broker=broker,
        server=server,
        request_path="/infer",
        method="POST",
        content_type="application/json",
        app=APP,
        batch_source=broker.batch_lease,
        batch_broker=_BatchBrokerView(broker),
        batch_objects=batch_objects,
        batch_call_record=_make_batch_call_record(
            server, request_path="/infer", method="POST", content_type="application/json"
        ),
        batch_output_partition_size=10,
        max_blank_retries=1,
        infer_presence_poll_s=0.01,
        batch_heartbeat_s=0.01,
    )
    try:
        # The infer lane drains immediately (empty infer queue); the batch lane leases the
        # one file via the broker, fetches input via the presigned body_url, processes, writes.
        import asyncio

        await asyncio.wait_for(adapter.run(), timeout=10.0)
    finally:
        await broker.aclose()
        await server.aclose()

    # --- Assert: output parts exist and status reaches completed. ---
    output_keys = await store.list_keys(f"AppData/{APP}/batch/{job_id}/output/")
    assert output_keys, "expected at least one output part written by the adapter"

    # Status endpoint (real aggregate) reflects completion.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gw") as gw:
        status = (await gw.get(f"/v1/{APP}/batch/{job_id}")).json()
    assert status["state"] == "completed", status
    assert status["records_done"] == 3
    assert status["records_total"] == 3
    assert status["files_done"] == 1


def _mint() -> str:
    from sluice_core.auth import mint_worker_token

    return mint_worker_token(app=APP, worker_id="w-e2e", key=KEY)


def _recording_model_server(seen: list[int], *, per_record_delay_s: float = 0.0) -> httpx.AsyncClient:
    """Local model server that records the integer index ``x`` of every record it processes.

    Each input record is ``{"_rid": "r<i>", "x": <i>}`` (see the resume/heartbeat seeds). The
    server appends ``x`` to ``seen`` so a test can assert exactly which record indices the
    adapter actually pushed through the model — the load-bearing proof that ``resume_from`` was
    honoured (skipped records never reach the server). An optional per-record delay makes
    processing of a multi-record file outlast a short heartbeat interval.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, content=b"ok")
        if per_record_delay_s:
            await asyncio.sleep(per_record_delay_s)
        record = json.loads(request.content)
        seen.append(int(record["x"]))
        return httpx.Response(200, json={"echo": record.get("_rid", "?"), "x": record.get("x")})

    return httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handler))


async def _drive_gateway_submit(gateway: FastAPI, store: FakeObjectStore, *, n_records: int) -> str:
    """Create + upload + seed input + submit a batch job via the REAL gateway router.

    Returns the ``job_id``. Writes the real manifest (so ``aggregate_status`` has its
    source-of-truth file set), seeds the ``n_records``-record JSONL into the shared store (the
    real client would PUT it directly via the presigned URL — submit's ``input_exists`` check
    requires it present), and enqueues exactly one ``{app}-batch`` message. Same path the
    keystone e2e test drives. Input records are ``{"_rid": "r<i>", "x": <i>}``.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gw") as gw:
        job_id = (await gw.post(f"/v1/{APP}/batch")).json()["job_id"]
        upload = (await gw.post(f"/v1/{APP}/batch/{job_id}/upload-url", json={"filename": "data.jsonl"})).json()
        assert "url" in upload
        records = [json.dumps({"_rid": f"r{i}", "x": i}).encode() for i in range(n_records)]
        await store.put(f"AppData/{APP}/batch/{job_id}/input/data.jsonl", b"\n".join(records))
        submit = await gw.post(f"/v1/{APP}/batch/{job_id}/submit", json={"files": ["data.jsonl"]})
        assert submit.status_code == 200 and submit.json()["submitted"] == 1
    return job_id


def _build_batch_adapter(
    *,
    broker: BrokerClient,
    server: httpx.AsyncClient,
    batch_heartbeat_s: float,
) -> Adapter:
    """Construct the adapter batch lane exactly the way ``_amain`` does (broker-only writer)."""
    return Adapter(
        broker=broker,
        server=server,
        request_path="/infer",
        method="POST",
        content_type="application/json",
        app=APP,
        batch_source=broker.batch_lease,
        batch_broker=_BatchBrokerView(broker),
        batch_objects=BrokerBatchWriter(broker, app=APP),
        batch_call_record=_make_batch_call_record(
            server, request_path="/infer", method="POST", content_type="application/json"
        ),
        batch_output_partition_size=10,
        max_blank_retries=1,
        infer_presence_poll_s=0.01,
        batch_heartbeat_s=batch_heartbeat_s,
    )


async def test_should_resume_from_checkpoint_when_file_redelivered_after_partial_progress() -> None:
    """I-1 headline correctness: a redelivered file resumes from its checkpoint, not from 0.

    Simulates a prior VM dying after its first partition: a ``status/data.jsonl.json`` already
    exists with ``records_done=2`` for a 5-record input file. The file is then leased + processed
    through the REAL broker batch route + adapter batch lane (the ``_amain`` construction). The
    adapter must read the checkpoint via ``BatchObjects.get_file_status`` and pass ``resume_from=2``
    to ``BatchFileProcessor.process`` so only records 2,3,4 hit the model.

    NON-VACUITY: if the adapter ignored the checkpoint and reprocessed from 0, ``seen`` would be
    [0,1,2,3,4] and the ``== [2, 3, 4]`` assertion below would fail. The proof is end-to-end:
    the recorder sits in the real local model server, downstream of the real resume_from plumbing.
    """
    store = _PresignedStore()
    queue = FakeQueue()
    gateway = _gateway_app(store, queue)

    # Real gateway flow seeds the 5-record input, writes the manifest, enqueues one batch message.
    job_id = await _drive_gateway_submit(gateway, store, n_records=5)

    # Seed a PRE-EXISTING checkpoint with records_done=2 via the REAL BatchObjects writer —
    # as if a prior VM died after flushing its first partition (records 0,1 done).
    batch_objects = BatchObjects(store=store)
    await batch_objects.put_file_status(
        APP,
        job_id,
        BatchFileStatus(
            file="data.jsonl",
            state="running",
            records_total=5,
            records_done=2,
            records_failed=0,
            updated_at=time.time(),
        ),
    )
    # Sanity: the seed is genuinely present at the real status key (not vacuous).
    assert await store.exists(status_key(APP, job_id, "data.jsonl"))

    broker = BrokerClient(base_url="http://gw", token=_mint(), transport=_worker_transport(gateway, store))
    seen: list[int] = []
    server = _recording_model_server(seen)
    adapter = _build_batch_adapter(broker=broker, server=server, batch_heartbeat_s=0.5)
    try:
        await asyncio.wait_for(adapter.run(), timeout=10.0)
    finally:
        await broker.aclose()
        await server.aclose()

    # Records 0 and 1 were skipped (resume_from=2 honoured end-to-end); only 2,3,4 processed.
    assert seen == [2, 3, 4], seen

    # Final status is completed with records_done=5 (NOT 3) — the file is whole, not the slice.
    final = await batch_objects.get_file_status(APP, job_id, "data.jsonl")
    assert final is not None
    assert final.state == "completed"
    assert final.records_done == 5
    assert final.records_failed == 0

    # The resumed output part covers the resumed range: a single part starting at offset 2.
    output_keys = await store.list_keys(f"AppData/{APP}/batch/{job_id}/output/")
    assert output_part_key(APP, job_id, "data.jsonl", 2) in output_keys, output_keys

    # Aggregate status (real endpoint) reflects the whole-file completion.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gw") as gw:
        status = (await gw.get(f"/v1/{APP}/batch/{job_id}")).json()
    assert status["state"] == "completed", status
    assert status["records_done"] == 5
    assert status["records_total"] == 5
    assert status["files_done"] == 1


class _ExtendCountingBrokerClient(BrokerClient):
    """BrokerClient that counts batch_extend calls while still hitting the REAL extend route.

    The heartbeat task calls ``_BatchBrokerView.extend`` → ``BrokerClient.batch_extend`` →
    ``POST /internal/v1/batch/extend``. Subclassing (rather than stubbing) keeps the real route
    in the path; we only observe the call count, proving the heartbeat fires through the batch
    lane, not a hand-rolled fake.
    """

    extend_calls: int = 0

    async def batch_extend(self, lease_ids: list[str]) -> None:
        self.extend_calls += 1
        await super().batch_extend(lease_ids)


async def test_should_heartbeat_extend_lease_while_processing_batch_file() -> None:
    """I-2: the batch heartbeat extends the lease via the real broker while a file is processing.

    The file takes longer than ``batch_heartbeat_s`` to process (a per-record delay), so the
    heartbeat task must fire ``batch_extend`` at least once during ``_process_batch_file``. We
    count calls on a BrokerClient subclass that still POSTs to the real ``/internal/v1/batch/extend``
    route — proving the heartbeat-extend mechanism is wired through the BATCH path end-to-end.
    """
    store = _PresignedStore()
    queue = FakeQueue()
    gateway = _gateway_app(store, queue)

    await _drive_gateway_submit(gateway, store, n_records=3)

    broker = _ExtendCountingBrokerClient(
        base_url="http://gw", token=_mint(), transport=_worker_transport(gateway, store)
    )
    seen: list[int] = []
    # 50ms per record × 3 records ≈ 150ms processing; heartbeat every 20ms ⇒ multiple extends.
    server = _recording_model_server(seen, per_record_delay_s=0.05)
    adapter = _build_batch_adapter(broker=broker, server=server, batch_heartbeat_s=0.02)
    try:
        await asyncio.wait_for(adapter.run(), timeout=10.0)
    finally:
        await broker.aclose()
        await server.aclose()

    # All three records were processed, and the lease was extended at least once mid-file.
    assert seen == [0, 1, 2], seen
    assert broker.extend_calls >= 1, broker.extend_calls
