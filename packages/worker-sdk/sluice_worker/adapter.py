"""Sidecar adapter: feed a packed HTTP model server (SamServe-style) from the broker.

The model server runs in the same unit (pod/VM) and packs its own replicas on the GPU. This
adapter holds the short-lived JWT and does the queue work the server doesn't know about: lease ->
fetch body via signed URL -> POST the body verbatim to the local server -> store the response
verbatim -> ack. Verbatim passthrough (ADR/spec): the queue body is the request payload and the
response is the result; the adapter is model-agnostic.

Dual-source mode (batch backfill) — spec §3.3 "on-box dual-source scheduler":
When ``batch_source`` / ``batch_broker`` / ``batch_objects`` / ``batch_call_record`` are provided,
the adapter feeds the SAME local model server from two lanes under ONE shared concurrency budget:

  * **Shared semaphore.** A single ``asyncio.Semaphore(put_concurrency)`` governs ALL in-flight
    calls to the local server — infer (``server.request``) and batch (``call_record``) alike. The
    total number of concurrent in-flight model calls NEVER exceeds ``put_concurrency``; there is no
    separate per-lane budget.
  * **Scheduler-driven slot allocation.** Each freed slot's infer-vs-batch choice is made by
    :class:`DualSourceScheduler`: prefer infer; backfill batch in idle capacity; after
    ``starve_grace_s`` of no batch progress under sustained infer, reserve >=1 slot for batch (the
    anti-starvation floor).
  * **Full-yield, never cancel.** While infer items are present, freed slots go to infer and batch
    records WAIT at the per-record gate (except the one reserved floor slot). In-flight work is
    never cancelled — already-dispatched infer or batch records run to completion.
  * **Per-record gate.** ``BatchFileProcessor.process()`` owns the per-file record loop; the adapter
    injects an ``acquire_slot`` gate that (a) acquires the shared semaphore and (b) only admits the
    record when the scheduler grants the slot to batch. This is how the file loop shares the one
    budget and yields to infer without the adapter driving records one-by-one.
  * **Infer presence.** Refreshed on a poll interval (``infer_presence_poll_s``, default 15 s) via
    ``scheduler.note_infer_present(...)`` from the latest infer lease observation.

Poison-file guard (spec §8):
If a leased batch file message has ``receive_count > MAX_FILE_REDELIVERIES``, the file is marked
``failed`` in the batch store and the message is acked (not nacked) so it is not redelivered again.

Infer-only mode (existing behaviour preserved):
When the batch kwargs are not supplied, ``run()`` delegates to the original single-source
``run_dispatch`` — existing tests are unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from sluice_core.batch_models import BatchFileStatus
from sluice_core.compression import gzip_bytes

from .scheduler import DualSourceScheduler

logger = logging.getLogger(__name__)

MAX_FILE_REDELIVERIES: int = 3
"""How many times a batch file message may be redelivered before it is marked failed."""

_BATCH_HEARTBEAT_S: float = 30.0
"""How often (seconds) to extend the lease on a held batch file message during processing."""

_DEFAULT_PUT_CONCURRENCY: int = 8
"""Shared in-flight model-call budget (spec §3.3) when not overridden."""

_DEFAULT_STARVE_GRACE_S: float = 7 * 60.0
"""Anti-starvation floor trigger (spec §3.4 ``starveGraceMin``)."""


class Adapter:
    def __init__(
        self,
        *,
        broker,
        server: httpx.AsyncClient,
        request_path: str = "/",
        method: str = "POST",
        content_type: str = "application/octet-stream",
        health_path: str = "/healthz",
        ready_timeout_s: int = 600,
        concurrency: int = 1,
        max_jobs: int = 0,
        max_blank_retries: int = 3,
        poll_s: float = 2.0,
        # --- dual-source batch kwargs (all optional; omit for infer-only mode) ---
        app: str = "",
        batch_source: Callable[[int], Awaitable[list[Any]]] | None = None,
        batch_broker: Any | None = None,
        batch_objects: Any | None = None,
        batch_call_record: Callable[[bytes], Awaitable[bytes]] | None = None,
        batch_output_partition_size: int = 1000,
        put_concurrency: int | None = None,
        starve_grace_s: float = _DEFAULT_STARVE_GRACE_S,
        infer_presence_poll_s: float = 15.0,
        batch_heartbeat_s: float = _BATCH_HEARTBEAT_S,
        infer_inflight_hooks: tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        self._broker = broker
        self._server = server
        self._request_path = request_path
        self._method = method
        self._content_type = content_type
        self._health_path = health_path
        self._ready_timeout_s = ready_timeout_s
        self._concurrency = max(concurrency, 1)
        self._max_jobs = max_jobs
        self._max_blank_retries = max_blank_retries
        self._poll_s = poll_s
        self._stop = False
        # batch
        self._app = app
        self._batch_source = batch_source
        self._batch_broker = batch_broker
        self._batch_objects = batch_objects
        self._batch_call_record = batch_call_record
        self._batch_output_partition_size = batch_output_partition_size
        # The shared model-call budget. Defaults to max(concurrency, _DEFAULT) so the
        # single semaphore is at least as wide as the infer concurrency knob.
        self._put_concurrency = (
            put_concurrency if put_concurrency is not None else max(self._concurrency, _DEFAULT_PUT_CONCURRENCY)
        )
        self._starve_grace_s = starve_grace_s
        self._infer_presence_poll_s = infer_presence_poll_s
        self._batch_heartbeat_s = batch_heartbeat_s
        # Optional test hooks: awaited on entry/exit of each infer model call so a
        # test can observe true in-flight overlap across the shared semaphore.
        self._infer_inflight_hooks = infer_inflight_hooks
        # Shared dual-source state (created in run() once the loop is live).
        self._slots: asyncio.Semaphore | None = None
        self._scheduler: DualSourceScheduler | None = None
        self._in_flight_infer: int = 0
        # Wall-clock of the moment batch last went idle (no record admitted); None
        # while batch is actively running. Drives the scheduler's starvation floor.
        self._batch_idle_since: float | None = None

    @property
    def _batch_mode(self) -> bool:
        return (
            self._batch_source is not None
            and self._batch_broker is not None
            and self._batch_objects is not None
            and self._batch_call_record is not None
        )

    async def wait_ready(self) -> bool:
        """Block until the local model server answers 200 on health_path, or time out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout_s
        while loop.time() < deadline:
            try:
                resp = await self._server.get(self._health_path)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(self._poll_s)
        raise TimeoutError(f"model server not ready within {self._ready_timeout_s}s")

    async def _safe_nack(self, lease_id: str) -> None:
        try:
            await self._broker.nack(lease_id)
        except Exception:
            # A lost nack just lets the lease lapse; surface it so it is observable.
            logger.warning("infer nack failed for lease %s", lease_id, exc_info=True)

    async def _handle(self, item: dict[str, Any]) -> None:
        # The dispatch engine never sees exceptions from here (that would leak the lease); each path
        # nacks at most once (a failing nack must not trigger a second).
        lease_id = item["lease_id"]
        try:
            body = await self._broker.get(item["body_url"])
            resp = await self._server.request(
                self._method, self._request_path, content=body, headers={"content-type": self._content_type}
            )
        except Exception:
            await self._safe_nack(lease_id)
            return
        if 200 <= resp.status_code < 300:
            try:
                # ALWAYS gzip (both archetypes — matches worker.py), so the always-`.gz` storage key
                # (inference_objects.result_key) is never a lie: a bucket reader or a batch presigned-
                # download client can gunzip by suffix without out-of-band metadata (commit 6b677e1).
                # The gateway streams it gzipped (Content-Encoding) or inflates for non-gzip clients.
                await self._broker.put(item["result_url"], gzip_bytes(resp.content))
                await self._broker.ack(lease_id)
            except Exception:
                await self._safe_nack(lease_id)
        else:
            await self._safe_nack(lease_id)

    def request_stop(self) -> None:
        self._stop = True

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

    # ------------------------------------------------------------------
    # Dual-source coordination (shared semaphore + scheduler)
    # ------------------------------------------------------------------

    async def _infer_call(self, item: dict[str, Any]) -> None:
        """An infer item under the shared semaphore.

        Holds one shared slot for the lifetime of the model call. Infer-presence
        accounting (``_in_flight_infer``) is updated so the scheduler/floor can see
        how many of ``put_concurrency`` are held by infer.
        """
        assert self._slots is not None
        await self._slots.acquire()
        self._in_flight_infer += 1
        enter = exit_ = None
        if self._infer_inflight_hooks is not None:
            enter, exit_ = self._infer_inflight_hooks
        try:
            if enter is not None:
                await enter()
            try:
                await self._handle(item)
            finally:
                if exit_ is not None:
                    await exit_()
        finally:
            self._in_flight_infer -= 1
            self._slots.release()

    @contextlib.asynccontextmanager
    async def _batch_slot(self) -> AsyncIterator[None]:
        """Per-record gate injected into ``BatchFileProcessor.process()``.

        Admits a batch record only when the scheduler grants the slot to batch
        (full-yield to infer), then holds one shared semaphore permit across the
        model call. Acquiring the permit and only releasing on exit is what bounds
        total in-flight model calls (infer + batch) to ``put_concurrency``.
        """
        assert self._slots is not None and self._scheduler is not None
        # Wait until the scheduler grants the slot to batch. Re-checked each time a
        # slot frees up; under sustained infer the starvation floor eventually wins.
        while not self._stop:
            decision = self._scheduler.next_source(
                infer_available=self._scheduler.infer_present,
                batch_available=True,
                batch_idle_since=self._batch_idle_since,
            )
            if decision == "batch":
                break
            # Infer is preferred for the next free slot; yield and re-evaluate after
            # the presence poll / an in-flight call completes. Never cancel in-flight.
            await asyncio.sleep(self._infer_presence_poll_s)
        await self._slots.acquire()
        # Batch is now actively running on a slot; clear the idle marker so the
        # starvation floor only fires after sustained future starvation.
        self._batch_idle_since = None
        try:
            yield
        finally:
            self._slots.release()
            # Mark the moment this slot went idle; the next gate iteration uses it to
            # detect starvation if infer keeps claiming the freed slots.
            self._batch_idle_since = time.monotonic()

    async def _infer_presence_poll(self) -> None:
        """Refresh ``scheduler.infer_present`` on a fixed cadence (spec §3.3).

        Presence is read from a peek on the infer queue when the broker supports it;
        otherwise it falls back to ``_in_flight_infer > 0`` (a held infer slot proves
        recent infer work). Stops when the adapter stops.
        """
        assert self._scheduler is not None
        while not self._stop:
            present = await self._peek_infer_present()
            self._scheduler.note_infer_present(present)
            await asyncio.sleep(self._infer_presence_poll_s)

    async def _peek_infer_present(self) -> bool:
        """Best-effort: is there infer work to prefer right now?

        Uses ``broker.depth()`` when available (a non-mutating peek); otherwise
        treats a held infer slot as presence.
        """
        depth = getattr(self._broker, "depth", None)
        if callable(depth):
            try:
                d = await depth()
                visible = getattr(d, "visible", None)
                if visible is not None:
                    return bool(visible) or self._in_flight_infer > 0
            except Exception:
                logger.warning("infer depth peek failed", exc_info=True)
        return self._in_flight_infer > 0

    async def _run_infer_lane(self) -> int:
        """Keep-busy infer dispatch under the SHARED semaphore.

        Mirrors ``run_dispatch`` but each in-flight item holds a shared-semaphore
        permit (via ``_infer_call``) so it shares the one budget with batch. Returns
        the number of infer items processed.
        """
        inflight: set[asyncio.Task[None]] = set()
        processed = 0
        blank = 0
        while (
            not self._stop and blank < self._max_blank_retries and (self._max_jobs == 0 or processed < self._max_jobs)
        ):
            # Lease only what the shared budget can admit; never lease more than the
            # free slots so we don't hold a backlog ahead of the scheduler.
            free = self._put_concurrency - self._in_flight_infer - len(inflight)
            if self._max_jobs:
                free = min(free, self._max_jobs - processed - len(inflight))
            items = await self._broker.lease(free) if free > 0 else []
            # Refresh presence from this lease observation between scheduled polls.
            if self._scheduler is not None:
                self._scheduler.note_infer_present(bool(items) or self._in_flight_infer > 0)
            if items:
                blank = 0
                inflight.update(asyncio.create_task(self._infer_call(it)) for it in items)
            elif not inflight:
                blank += 1
                await asyncio.sleep(self._poll_s if self._poll_s else 0)
                continue
            if inflight:
                done, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.exception()  # retrieve so a raised handle doesn't warn; handle owns nack
                processed += len(done)
        if inflight:
            results = await asyncio.gather(*inflight, return_exceptions=True)
            processed += len(results)
        return processed

    # ------------------------------------------------------------------
    # Batch lane
    # ------------------------------------------------------------------

    async def _process_batch_file(self, msg: Any) -> None:
        """Download, process (through the shared-semaphore gate), and ack one file.

        Heartbeats ``extend`` on the message lease every ``_batch_heartbeat_s`` seconds
        while ``BatchFileProcessor.process()`` runs.  A message redelivered too many
        times is marked failed and acked without processing (poison guard, spec §8).
        """
        from .batch_driver import BatchFileProcessor

        # Only reachable when _batch_mode is True; assert to narrow Optional types.
        assert self._batch_broker is not None
        assert self._batch_objects is not None
        assert self._batch_call_record is not None

        ack_token: str = msg.ack_token
        attrs: dict[str, str] = msg.attributes
        job_id = attrs.get("job_id", "")
        filename = attrs.get("file", "")
        body_url = attrs.get("body_url", "")
        app = self._app

        # --- Poison-file guard ---
        if msg.receive_count > MAX_FILE_REDELIVERIES:
            failed_status = BatchFileStatus(
                file=filename,
                state="failed",
                records_total=0,
                records_done=0,
                records_failed=0,
                updated_at=time.time(),
            )
            try:
                await self._batch_objects.put_file_status(app, job_id, failed_status)
            except Exception:
                logger.warning("put_file_status(failed) failed for %s/%s", job_id, filename, exc_info=True)
            try:
                await self._batch_broker.ack(ack_token)
            except Exception:
                logger.warning("ack(poison) failed for %s", ack_token, exc_info=True)
            return

        # --- Download file body ---
        try:
            raw_body = await self._batch_broker.get(body_url)
        except Exception:
            logger.warning("batch body download failed for %s", body_url, exc_info=True)
            try:
                await self._batch_broker.nack(ack_token)
            except Exception:
                logger.warning("nack failed for %s", ack_token, exc_info=True)
            return

        lines = [ln for ln in raw_body.splitlines() if ln.strip()]

        # --- Determine resume point (spec §7) ---
        resume_from = 0
        try:
            existing = await self._batch_objects.get_file_status(app, job_id, filename)
            if existing is not None:
                resume_from = existing.records_done
        except Exception:
            logger.warning("get_file_status failed for %s/%s; resuming from 0", job_id, filename, exc_info=True)

        processor = BatchFileProcessor(
            call_record=self._batch_call_record,
            writer=self._batch_objects,
            output_partition_size=self._batch_output_partition_size,
        )

        heartbeat_task = asyncio.create_task(self._heartbeat_batch_lease(ack_token, self._batch_heartbeat_s))
        try:
            await processor.process(
                app, job_id, filename, lines, resume_from=resume_from, acquire_slot=self._batch_slot
            )
        except Exception:
            logger.warning("batch file processing failed for %s/%s", job_id, filename, exc_info=True)
            heartbeat_task.cancel()
            try:
                await self._batch_broker.nack(ack_token)
            except Exception:
                logger.warning("nack failed for %s", ack_token, exc_info=True)
            return
        else:
            heartbeat_task.cancel()
            try:
                await self._batch_broker.ack(ack_token)
            except Exception:
                logger.warning("ack failed for %s", ack_token, exc_info=True)

    async def _heartbeat_batch_lease(self, ack_token: str, interval_s: float) -> None:
        """Periodically extend the batch file lease so it is not reclaimed during processing."""
        assert self._batch_broker is not None
        try:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self._batch_broker.extend([ack_token])
                except Exception:
                    logger.warning("extend_lease failed for %s", ack_token, exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _run_batch_lane(self) -> None:
        """Drain the batch queue one file at a time; stop on signal or empty queue."""
        assert self._batch_source is not None
        blank = 0
        # Batch starts idle: if infer is busy from the start the floor still applies.
        self._batch_idle_since = time.monotonic()
        while not self._stop and blank < self._max_blank_retries:
            msgs = await self._batch_source(1)
            if not msgs:
                blank += 1
                await asyncio.sleep(self._poll_s if self._poll_s else 0)
                continue
            blank = 0
            for msg in msgs:
                await self._process_batch_file(msg)

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run(self) -> int:
        from .dispatch import run_dispatch

        self._install_signals()

        if not self._batch_mode:
            # Infer-only mode: behaviour is byte-for-byte identical to the original.
            return await run_dispatch(
                lease=self._broker.lease,
                handle=self._handle,
                concurrency=self._concurrency,
                should_stop=lambda: self._stop,
                max_jobs=self._max_jobs,
                max_blank_retries=self._max_blank_retries,
            )

        # Dual-source mode: ONE shared semaphore + scheduler arbitrate both lanes.
        self._slots = asyncio.Semaphore(self._put_concurrency)
        self._scheduler = DualSourceScheduler(
            put_concurrency=self._put_concurrency,
            starve_grace_s=self._starve_grace_s,
            now=time.monotonic,
        )
        poll_task = asyncio.create_task(self._infer_presence_poll())
        try:
            results = await asyncio.gather(
                self._run_infer_lane(),
                self._run_batch_lane(),
                return_exceptions=True,
            )
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        for r in results:
            if isinstance(r, BaseException):
                raise r
        return results[0] if isinstance(results[0], int) else 0


class _BatchBrokerView:
    """Adapts a :class:`BrokerClient`'s batch methods to the generic names the batch lane uses.

    The adapter's batch lane calls ``get``/``ack``/``extend``/``nack`` on its
    ``batch_broker``; the BrokerClient exposes those under ``batch_*`` (so they hit the
    ``/internal/v1/batch/*`` routes, not the infer routes). This view bridges the two.
    """

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    async def get(self, url: str) -> bytes:
        return bytes(await self._broker.get(url))

    async def ack(self, lease_id: str) -> None:
        await self._broker.batch_ack(lease_id)

    async def extend(self, lease_ids: list[str]) -> None:
        await self._broker.batch_extend(lease_ids)

    async def nack(self, lease_id: str) -> None:
        await self._broker.batch_nack(lease_id)


def _make_batch_call_record(
    server: httpx.AsyncClient, *, request_path: str, method: str, content_type: str
) -> Callable[[bytes], Awaitable[bytes]]:
    """Build the per-record inference callable: POST one record to the local model server.

    Mirrors the infer ``_handle`` server call (verbatim passthrough). Raises on a non-2xx
    so ``BatchFileProcessor`` records the record as a per-record error (spec §8).
    """

    async def call_record(record: bytes) -> bytes:
        resp = await server.request(method, request_path, content=record, headers={"content-type": content_type})
        resp.raise_for_status()
        return bytes(resp.content)

    return call_record


async def _amain() -> None:
    from .broker_client import BrokerClient
    from .config import WorkerSettings

    ws = WorkerSettings()
    broker = BrokerClient(base_url=ws.broker_url, token=ws.broker_token)
    server = httpx.AsyncClient(base_url=f"http://localhost:{ws.server_port}", timeout=300.0)

    # Batch lane: only constructed when the app declares a batch block (WORKER__BATCH_ENABLED). The
    # worker holds ONLY its JWT — output parts go to broker-minted presigned PUTs (streamed from the
    # spilled temp file) and per-file status is broker-proxied; no object-store credentials anywhere
    # on the VM (ADR-002/008). Input is fetched via the broker's presigned body_url.
    batch_kwargs: dict[str, Any] = {}
    if ws.batch_enabled:
        from .batch_writer import BrokerBatchWriter

        batch_objects = BrokerBatchWriter(broker, app=ws.app)
        batch_kwargs = {
            "app": ws.app,
            "batch_source": broker.batch_lease,
            "batch_broker": _BatchBrokerView(broker),
            "batch_objects": batch_objects,
            "batch_call_record": _make_batch_call_record(
                server,
                request_path=ws.server_request_path,
                method=ws.server_method,
                content_type=ws.server_content_type,
            ),
            "batch_output_partition_size": ws.batch_output_partition_size,
            "put_concurrency": ws.put_concurrency,
            "starve_grace_s": ws.starve_grace_s,
            "infer_presence_poll_s": ws.infer_presence_poll_s,
            "batch_heartbeat_s": ws.batch_heartbeat_s,
        }

    adapter = Adapter(
        broker=broker,
        server=server,
        request_path=ws.server_request_path,
        method=ws.server_method,
        content_type=ws.server_content_type,
        health_path=ws.server_health_path,
        ready_timeout_s=ws.server_ready_timeout_s,
        concurrency=ws.concurrency,
        max_jobs=ws.max_jobs,
        max_blank_retries=ws.max_blank_retries,
        **batch_kwargs,
    )
    try:
        await adapter.wait_ready()
        await adapter.run()
    finally:
        await broker.aclose()
        await server.aclose()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
