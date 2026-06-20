"""BatchFileProcessor — parse, partition, checkpoint, and resume a single JSONL file.

Output envelope (per output record):
- Success: ``{"_rid": "<rid>", "result": <decoded JSON or base64 string>}``
- Failure: ``{"_rid": "<rid>", "error": "<message>"}``

The ``_rid`` is taken from the input record's top-level ``_rid`` field (if a non-empty
string), otherwise falls back to ``f"{filename}:{lineno}"`` where ``lineno`` is the
0-based index within the full ``lines`` list (not the resumed slice).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from sluice_core.batch_models import BatchFileStatus


def _spill_open() -> tuple[Any, str]:
    """Open a fresh temp file for the current output partition (kept open across record writes)."""
    handle = tempfile.NamedTemporaryFile(prefix="sluice-batch-", suffix=".jsonl", delete=False)
    return handle, handle.name


def _spill_write(handle: Any, data: bytes, first: bool) -> None:
    if not first:
        handle.write(b"\n")
    handle.write(data)


def _unlink_quiet(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


#: An async context manager factory awaited per record before the model call and
#: released after.  The dual-source adapter injects a gate that acquires the shared
#: ``put_concurrency`` semaphore and admits the record only when the scheduler grants
#: the slot to batch (so batch yields to infer).  Defaults to a no-op pass-through.
AcquireSlot = Callable[[], "contextlib.AbstractAsyncContextManager[None]"]


@contextlib.asynccontextmanager
async def _noop_slot() -> AsyncIterator[None]:
    """Default per-record gate: grants the slot immediately, no shared budget."""
    yield


class _Writer(Protocol):
    async def put_output_part(self, app: str, job_id: str, filename: str, start_offset: int, body: str) -> None: ...

    async def put_file_status(self, app: str, job_id: str, st: BatchFileStatus) -> None: ...

    async def get_file_status(self, app: str, job_id: str, filename: str) -> BatchFileStatus | None: ...


def _extract_rid(record_bytes: bytes, filename: str, lineno: int) -> str:
    """Return the ``_rid`` from the record, falling back to ``filename:lineno``."""
    try:
        doc = json.loads(record_bytes)
    except (json.JSONDecodeError, ValueError):
        return f"{filename}:{lineno}"
    if isinstance(doc, dict):
        rid = doc.get("_rid")
        if isinstance(rid, str) and rid:
            return rid
    return f"{filename}:{lineno}"


def _make_result_value(result_bytes: bytes) -> Any:
    """Decode result bytes to a JSON-safe value.

    Tries JSON first; if the bytes are not valid JSON, encodes them as a
    base64 string so the output remains JSON-serialisable.
    """
    try:
        return json.loads(result_bytes)
    except (json.JSONDecodeError, ValueError):
        return base64.b64encode(result_bytes).decode()


class BatchFileProcessor:
    """Process a single JSONL file: call the model per record, partition output, checkpoint.

    Args:
        call_record: Async callable ``(record_bytes) -> result_bytes`` — the per-record
            inference.  On exception the record is written as an error entry.
        writer: Duck-typed object exposing ``put_output_part``, ``put_file_status``, and
            ``get_file_status`` (matches ``BatchObjects`` and ``FakeWriter`` in tests).
        output_partition_size: Number of output records per flushed part.
    """

    def __init__(
        self,
        *,
        call_record: Callable[[bytes], Awaitable[bytes]],
        writer: _Writer,
        output_partition_size: int,
    ) -> None:
        self._call_record = call_record
        self._writer = writer
        self._output_partition_size = output_partition_size

    async def process(
        self,
        app: str,
        job_id: str,
        filename: str,
        lines: list[bytes],
        *,
        resume_from: int,
        acquire_slot: AcquireSlot | None = None,
    ) -> BatchFileStatus:
        """Process ``lines[resume_from:]`` and return the final per-file status.

        Already-processed records (indices 0 .. resume_from-1) are skipped.
        Each output part is written every ``output_partition_size`` records; a
        checkpoint status is written after each flush.  At the end the tail part
        (if any) is flushed and a final status with state ``completed`` or
        ``partial`` is returned.

        ``acquire_slot`` is an async-context-manager factory entered around each
        per-record model call.  The dual-source adapter injects a gate that
        acquires the shared ``put_concurrency`` semaphore and admits the record
        only when the on-box scheduler grants the slot to batch, so batch records
        yield to infer.  When omitted, a no-op pass-through is used (no shared
        budget) — preserving the standalone driver semantics.
        """
        slot = acquire_slot if acquire_slot is not None else _noop_slot
        records_failed = 0
        # The current output partition is spilled to a temp FILE as records complete — a large result
        # set never accumulates in RAM (only ~one record at a time). The file is streamed to the writer
        # at flush, then unlinked. All file I/O is off-thread so it never blocks the event loop.
        spill_handle: Any = None
        spill_path: str = ""
        spill_count = 0
        # The absolute record index of the first record in the current partition.
        partition_start: int = resume_from

        for abs_idx in range(resume_from, len(lines)):
            record_bytes = lines[abs_idx]
            rid = _extract_rid(record_bytes, filename, abs_idx)

            try:
                async with slot():
                    result_bytes = await self._call_record(record_bytes)
                output_entry: dict[str, Any] = {"_rid": rid, "result": _make_result_value(result_bytes)}
            except Exception as exc:  # noqa: BLE001
                output_entry = {"_rid": rid, "error": str(exc)}
                records_failed += 1

            if spill_handle is None:
                spill_handle, spill_path = await asyncio.to_thread(_spill_open)
            await asyncio.to_thread(_spill_write, spill_handle, json.dumps(output_entry).encode(), spill_count == 0)
            spill_count += 1

            # Flush when partition is full.
            if spill_count == self._output_partition_size:
                await asyncio.to_thread(spill_handle.close)
                await self._flush_partition(
                    app=app,
                    job_id=job_id,
                    filename=filename,
                    start_offset=partition_start,
                    spill_path=spill_path,
                    records_done_so_far=abs_idx + 1,
                    records_total=len(lines),
                    records_failed=records_failed,
                )
                partition_start = abs_idx + 1
                spill_handle, spill_path, spill_count = None, "", 0

        # Flush the tail partition (the still-open spill file, if any records remain unflushed).
        if spill_handle is not None and spill_count:
            await asyncio.to_thread(spill_handle.close)
            await self._flush_partition(
                app=app,
                job_id=job_id,
                filename=filename,
                start_offset=partition_start,
                spill_path=spill_path,
                records_done_so_far=len(lines),
                records_total=len(lines),
                records_failed=records_failed,
            )

        # Final status: mark as completed or partial.
        final_state = "partial" if records_failed > 0 else "completed"
        final_status = BatchFileStatus(
            file=filename,
            state=final_state,
            records_total=len(lines),
            records_done=len(lines),
            records_failed=records_failed,
            updated_at=time.time(),
        )
        await self._writer.put_file_status(app, job_id, final_status)
        return final_status

    async def _flush_partition(
        self,
        *,
        app: str,
        job_id: str,
        filename: str,
        start_offset: int,
        spill_path: str,
        records_done_so_far: int,
        records_total: int,
        records_failed: int,
    ) -> None:
        # OUTPUT first (durable), THEN the status checkpoint — status.records_done is never ahead of
        # durable output, so a crash between the two only causes the next worker to reprocess this one
        # partition (the offset-named key is overwritten, never duplicated). This is the resume invariant.
        try:
            await self._writer.put_output_part(app, job_id, filename, start_offset, spill_path)
        finally:
            await asyncio.to_thread(_unlink_quiet, spill_path)
        checkpoint = BatchFileStatus(
            file=filename,
            state="running",
            records_total=records_total,
            records_done=records_done_so_far,
            records_failed=records_failed,
            updated_at=time.time(),
        )
        await self._writer.put_file_status(app, job_id, checkpoint)
